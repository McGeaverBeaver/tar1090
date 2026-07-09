/* Flight route lookup — "where is this plane going to land?"
 *
 * Resolves a callsign to its filed origin → destination airports using the same public
 * route API the stock tar1090 UI uses (the adsb.im / api.adsb.lol "routeset" endpoint,
 * data from the community vrs-standing-data project). Lookups are batched into one POST,
 * deduped, and cached in localStorage (6 h for hits, 30 min for misses) so browsing stays
 * cheap and polite to the public service. Only scheduled (mostly airline) callsigns have
 * routes — GA, military and empty callsigns simply come back with no route.
 *
 * Public API (global FlightRoute):
 *   FlightRoute.get(callsign, lat, lon)     sync: route | null. A cache miss queues a
 *                                           batched lookup; onUpdate fires when it lands.
 *   FlightRoute.lookup(callsign, lat, lon)  async: resolves to route | null
 *   FlightRoute.onUpdate(cb)                cb() whenever new routes arrive (re-render hook)
 *   FlightRoute.dest(route)                 the landing airport (final leg) | null
 *   FlightRoute.html(route)                 'Sydney <small>(SYD)</small> → <b>Melbourne (MEL)</b>'
 *   FlightRoute.shortLabel(route)           '→ MEL' (for map / 3D-scene labels)
 * route: { airports: [{icao,iata,name,location}, ...], plausible }   (airports.length >= 2)
 *
 * The lat/lon (aircraft position) is optional; when given, the service uses it to pick the
 * right leg of multi-leg routes and to sanity-check the route (plausible=false -> shown "??").
 * To turn lookups off: localStorage.setItem('routeApi','off'), or set window.ROUTE_API_URL=''
 * before this script loads (to point at a different routeset server, set it to that URL).
 */
(function (global) {
  'use strict';
  const API = (typeof global.ROUTE_API_URL === 'string') ? global.ROUTE_API_URL
            : 'https://adsb.im/api/0/routeset';   // caching proxy in front of api.adsb.lol
  let off = !API;
  try { off = off || localStorage.getItem('routeApi') === 'off'; } catch (e) {}
  const TTL_HIT = 6 * 3600e3, TTL_MISS = 30 * 60e3, LS_KEY = 'routeCacheV1', LS_MAX = 500;

  const esc = s => (s == null ? '' : String(s)).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

  // trim + strip leading zeros in the numeric part, like tar1090 (QFA0424 -> QFA424)
  function norm(cs) {
    cs = (cs || '').trim().toUpperCase();
    const m = cs.match(/^([A-Z]*)([0-9]*)([A-Z]*)$/);
    if (!m) return cs;
    let num = m[2];
    while (num.length > 1 && num[0] === '0') num = num.slice(1);
    return m[1] + num + m[3];
  }

  let mem = {};                            // callsign -> { t: fetchedAtMs, r: route | null }
  try { const j = JSON.parse(localStorage.getItem(LS_KEY) || '{}'); if (j && typeof j === 'object') mem = j; } catch (e) {}
  function persist() {
    try {
      const keys = Object.keys(mem);
      if (keys.length > LS_MAX) {          // keep only the freshest entries
        keys.sort((a, b) => mem[b].t - mem[a].t);
        for (const k of keys.slice(LS_MAX)) delete mem[k];
      }
      localStorage.setItem(LS_KEY, JSON.stringify(mem));
    } catch (e) {}
  }

  const queue = new Map();                 // callsign -> { callsign, lat, lng }
  const waiters = new Map();               // callsign -> [resolve, ...]
  const cbs = [];
  let timer = null, inFlight = false;

  const fresh = e => e && (Date.now() - e.t) < (e.r ? TTL_HIT : TTL_MISS);

  function enqueue(cs, lat, lon) {
    if (off) return;
    if (!queue.has(cs)) queue.set(cs, { callsign: cs, lat: lat != null ? +lat : undefined, lng: lon != null ? +lon : undefined });
    if (!timer) timer = setTimeout(flush, 400);   // short window so a burst becomes ONE request
  }

  async function flush() {
    timer = null;
    if (inFlight) { timer = setTimeout(flush, 1000); return; }
    const batch = [...queue.values()].slice(0, 100);
    if (!batch.length) return;
    for (const b of batch) queue.delete(b.callsign);
    inFlight = true;
    let routes = null;
    try {
      const r = await fetch(API, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                   body: JSON.stringify({ planes: batch }) });
      if (r.ok) routes = await r.json();
    } catch (e) { /* network/CORS error -> cached as a miss; the short TTL retries later */ }
    inFlight = false;
    const byCs = {};
    if (Array.isArray(routes)) for (const rt of routes) {
      if (!rt || !rt.callsign) continue;
      const aps = (rt._airports || []).filter(a => a && (a.icao || a.iata));
      byCs[rt.callsign] = aps.length >= 2
        ? { airports: aps.map(a => ({ icao: a.icao, iata: a.iata, name: a.name, location: a.location })),
            plausible: !(rt.plausible === false || rt.plausible === 0) }
        : null;
    }
    for (const b of batch) {
      mem[b.callsign] = { t: Date.now(), r: byCs[b.callsign] || null };
      const w = waiters.get(b.callsign); waiters.delete(b.callsign);
      if (w) for (const res of w) res(mem[b.callsign].r);
    }
    persist();
    if (queue.size) timer = setTimeout(flush, 1200);
    for (const cb of cbs) { try { cb(); } catch (e) {} }
  }

  function get(callsign, lat, lon) {
    const cs = norm(callsign);
    if (!cs || off) return null;
    const e = mem[cs];
    if (fresh(e)) return e.r;
    enqueue(cs, lat, lon);
    return null;
  }
  function lookup(callsign, lat, lon) {
    const cs = norm(callsign);
    if (!cs || off) return Promise.resolve(null);
    const e = mem[cs];
    if (fresh(e)) return Promise.resolve(e.r);
    return new Promise(res => {
      if (!waiters.has(cs)) waiters.set(cs, []);
      waiters.get(cs).push(res);
      enqueue(cs, lat, lon);
    });
  }

  const dest  = r => (r && r.airports && r.airports.length >= 2) ? r.airports[r.airports.length - 1] : null;
  const code  = a => (a && (a.iata || a.icao)) || '?';
  const place = a => (a && (a.location || a.name)) || '';
  function html(r) {                       // full chain, landing airport emphasised
    if (!r || !r.airports || r.airports.length < 2) return '';
    const parts = r.airports.map((a, i) => {
      const s = `${esc(place(a) || code(a))} <small>(${esc(code(a))})</small>`;
      return i === r.airports.length - 1 ? `<b>${s}</b>` : s;
    });
    return (r.plausible ? '' : '<span title="reported position does not match this route">??</span> ') + parts.join(' → ');
  }
  const shortLabel = r => { const d = dest(r); return d ? '→ ' + code(d) : ''; };

  global.FlightRoute = { get, lookup, onUpdate: cb => cbs.push(cb), dest, html, shortLabel, code, place, enabled: !off };
})(window);
