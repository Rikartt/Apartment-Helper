/* Registers the service worker and shows a clear offline banner. */
(function () {
  var bar = null;
  function show(on) {
    if (on && !bar) {
      bar = document.createElement('div');
      bar.setAttribute('role', 'status');
      bar.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:99999;background:#c00;color:#fff;' +
        'font:12px "Courier New",monospace;padding:8px 12px;text-align:center;letter-spacing:.04em';
      bar.textContent = 'OFFLINE - no connection. Listings are never cached, so nothing new loads until you are back online.';
      document.body.appendChild(bar);
    } else if (!on && bar) { bar.remove(); bar = null; }
  }
  // "Updated" notice in the app's toast style (index.html #toasts/.toast); stays until used or dismissed
  function updated() {
    if (document.getElementById('sw-update')) return;
    var host = document.getElementById('toasts') || document.body;
    var el = document.createElement('div');
    el.id = 'sw-update';
    el.className = 'toast';   // #toasts is already an aria-live region
    if (host === document.body) {
      el.setAttribute('role', 'status');
      el.style.cssText = 'position:fixed;left:50%;bottom:16px;transform:translateX(-50%);z-index:3000;' +
        'background:#222;border:1px solid #444;border-left:3px solid #ffc107;color:#e0e0e0;' +
        'font:12px "Courier New",monospace;padding:8px 12px;display:flex;gap:12px;align-items:center';
    }
    var msg = document.createElement('span');
    msg.textContent = 'WGPendler was updated.';
    var go = document.createElement('button');
    go.type = 'button';
    go.textContent = 'Reload';
    go.onclick = function () { location.reload(); };
    var x = document.createElement('button');
    x.type = 'button';
    x.textContent = '\u2715';
    x.setAttribute('aria-label', 'Dismiss update notice');
    x.onclick = function () { el.remove(); };
    el.appendChild(msg); el.appendChild(go); el.appendChild(x);
    host.appendChild(el);
  }
  window.addEventListener('offline', function () { show(true); });
  window.addEventListener('online', function () { show(false); });
  if (!navigator.onLine) document.addEventListener('DOMContentLoaded', function () { show(true); });
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.addEventListener('message', function (e) {
      if (e.data && e.data.type === 'offline') show(true);
    });
    // After a code update (git pull + restart) sw.js changes, the new worker takes over and this
    // fires in every open page. Pages opened later already get the new files, so only those that
    // were running before (had a controller) are offered a reload. Nothing reloads by itself:
    // a half-written draft must never be lost.
    var hadController = !!navigator.serviceWorker.controller;
    navigator.serviceWorker.addEventListener('controllerchange', function () {
      if (hadController) updated();
      hadController = true;
    });
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/sw.js').then(function (reg) {
        // An installed PWA can stay open for days: look for a new version whenever it comes back
        // to the foreground (at most every 10 minutes; sw.js is a tiny no-cache request).
        var last = Date.now();
        document.addEventListener('visibilitychange', function () {
          if (document.visibilityState !== 'visible' || Date.now() - last < 600000) return;
          last = Date.now();
          reg.update().catch(function () {});
        });
      }).catch(function () {});
    });
  }
})();
