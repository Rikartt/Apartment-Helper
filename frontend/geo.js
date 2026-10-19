// Map geometry helpers for the "draw area" filter. Pure functions, no Leaflet, no DOM.
// A polygon is a list of [lat, lng] points (at least 3), implicitly closed.
(function (root) {
  // Ray casting (even-odd rule) on lng = x, lat = y. Fine at city scale: the polygon is
  // drawn on a Web-Mercator map, but over a few km its edges are straight in lat/lng too.
  function pointInPolygon(lat, lng, poly) {
    if (!Array.isArray(poly) || poly.length < 3 || lat == null || lng == null) return false;
    let inside = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      const [yi, xi] = poly[i], [yj, xj] = poly[j];
      if ((yi > lat) !== (yj > lat) && lng < (xj - xi) * (lat - yi) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }

  // A stored area is usable when it has >= 3 finite [lat, lng] points; anything else is dropped.
  function cleanPolygon(poly) {
    if (!Array.isArray(poly)) return null;
    const pts = poly.filter(p => Array.isArray(p) && p.length === 2 && p.every(Number.isFinite)
      && Math.abs(p[0]) <= 90 && Math.abs(p[1]) <= 180)
      .map(([a, b]) => [Math.round(a * 1e6) / 1e6, Math.round(b * 1e6) / 1e6]);
    return pts.length >= 3 ? pts : null;
  }

  const api = { pointInPolygon, cleanPolygon };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.WGGeo = api;
})(typeof window !== 'undefined' ? window : globalThis);
