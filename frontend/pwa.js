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
  window.addEventListener('offline', function () { show(true); });
  window.addEventListener('online', function () { show(false); });
  if (!navigator.onLine) document.addEventListener('DOMContentLoaded', function () { show(true); });
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.addEventListener('message', function (e) {
      if (e.data && e.data.type === 'offline') show(true);
    });
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/sw.js').catch(function () {});
    });
  }
})();
