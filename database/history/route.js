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
 *   FlightRoute.legHTML(route, opts)        the flight-leg visual: big codes, a track line with
 *                                           the plane at its real en-route progress, cities and a
 *                                           live "lands ≈ HH:MM" ETA. opts: {lat, lon, gs} of the
 *                                           aircraft now (all optional; ETA needs all three).
 *   FlightRoute.shortLabel(route)           '→ MEL' (for map / 3D-scene labels)
 * route: { airports: [{icao,iata,name,location,lat,lon}, ...], plausible }  (airports.length >= 2)
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
  const TTL_HIT = 6 * 3600e3, TTL_MISS = 30 * 60e3, LS_KEY = 'routeCacheV2', LS_MAX = 500;

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
        ? { airports: aps.map(a => ({ icao: a.icao, iata: a.iata, name: a.name, location: a.location,
                                      lat: a.lat, lon: a.lon })),
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
  const shortLabel = r => { const d = dest(r); return d ? '→ ' + code(d) : ''; };

  // ---- the flight-leg visual --------------------------------------------------------------
  // Boarding-pass style: SYD ——✈—— MEL with the plane at its true along-track progress, city
  // names underneath, and a live "lands ≈ HH:MM" ETA. Self-styling (injects its CSS once) so
  // the Live and History pages render it pixel-identically.
  const LEG_CSS = `
.fr-leg { padding: 3px 0 1px; }
.fr-row { display: flex; align-items: center; gap: 9px; }
.fr-code { font-size: 17px; font-weight: 800; letter-spacing: .1em; color: #e8edf5; line-height: 1.15; }
.fr-code.fr-dest { color: #6cc1ff; text-shadow: 0 0 14px rgba(108,193,255,.35); }
.fr-track { position: relative; flex: 1; height: 16px; min-width: 56px; }
.fr-base { position: absolute; left: 0; right: 0; top: 50%; border-top: 2px dotted #3a4558;
           transform: translateY(-50%); }
.fr-done { position: absolute; left: 0; top: 50%; height: 2px; transform: translateY(-50%);
           background: linear-gradient(90deg, #3f74d8, #6cc1ff); border-radius: 2px;
           box-shadow: 0 0 8px rgba(108,193,255,.55); }
.fr-dot { position: absolute; top: 50%; width: 5px; height: 5px; border-radius: 50%; }
.fr-dot.o { left: 0; transform: translate(-40%,-50%); background: #8fb7e8; }
.fr-dot.d { right: 0; transform: translate(40%,-50%); background: #10141c;
            border: 1.5px solid #6cc1ff; box-shadow: 0 0 6px rgba(108,193,255,.45); }
.fr-plane { position: absolute; top: 50%; transform: translate(-50%,-50%); width: 15px; height: 15px;
            color: #eaf3ff; filter: drop-shadow(0 0 5px rgba(108,193,255,.9)); }
.fr-plane svg { display: block; width: 100%; height: 100%; fill: currentColor; }
.fr-cities { display: flex; justify-content: space-between; align-items: baseline; gap: 8px;
             margin-top: 2px; font-size: 10.5px; color: #8a93a2; }
.fr-cities > span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fr-cities .fr-o, .fr-cities .fr-d { max-width: 42%; flex: 0 1 auto; }
.fr-cities .fr-d { text-align: right; }
.fr-cities .fr-m { color: #6cc1ff; font-weight: 600; flex: 1; text-align: center; }
.fr-warn { color: #ffb45e; font-size: 12px; font-weight: 700; cursor: help; margin-right: 5px; }
`;
  function ensureStyle() {
    if (typeof document === 'undefined' || document.getElementById('fr-style')) return;
    const s = document.createElement('style'); s.id = 'fr-style'; s.textContent = LEG_CSS;
    document.head.appendChild(s);
  }

  // right-pointing plane silhouette (inline SVG: the ✈ font glyph's base orientation
  // differs between platforms, which would leave the marker randomly tilted)
  const PLANE_SVG = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M19 10c0-.6-2.9-1.2-6.6-1.4L7 3.2l-2 .4 3.2 5L4 8.8 2.1 6.9l-1.4.3L2 10 .7 12.8l1.4.3L4 11.2l4.2.2-3.2 5 2 .4 5.4-5.4c3.7-.2 6.6-.8 6.6-1.4z"/></svg>';

  const KM = 6371, D2R = Math.PI / 180;
  function havKm(la1, lo1, la2, lo2) {
    const a = Math.sin((la2 - la1) * D2R / 2) ** 2
            + Math.cos(la1 * D2R) * Math.cos(la2 * D2R) * Math.sin((lo2 - lo1) * D2R / 2) ** 2;
    return 2 * KM * Math.asin(Math.sqrt(a));
  }

  // Along-track fraction and ETA for a plane at (lat,lon) doing gs knots on the o->d leg.
  // frac projects the plane onto the leg (law of cosines), so a plane abeam mid-route reads ~50%.
  function legProgress(o, d, lat, lon, gs) {
    if (lat == null || lon == null || o.lat == null || d.lat == null) return null;
    const total = havKm(o.lat, o.lon, d.lat, d.lon);
    if (total < 40) return null;                       // too short a leg to be meaningful
    const done = havKm(o.lat, o.lon, lat, lon);
    const remain = havKm(lat, lon, d.lat, d.lon);
    const frac = Math.max(0, Math.min(1, (done * done + total * total - remain * remain) / (2 * total * total)));
    let eta = null;
    if (gs != null && gs > 80 && frac < 0.99) {
      const h = remain / (gs * 1.852);                 // knots -> km/h
      if (h < 12) eta = Date.now() + h * 3600e3;
    }
    return { frac, remain, eta };
  }

  function legHTML(r, o) {
    if (!r || !r.airports || r.airports.length < 2) return '';
    ensureStyle();
    o = o || {};
    const ap = r.airports, org = ap[0], dst = ap[ap.length - 1];
    const p = legProgress(org, dst, o.lat, o.lon, o.gs);
    const pc = p ? Math.max(3, Math.min(97, Math.round(p.frac * 100))) : null;
    const warn = r.plausible ? ''
      : '<span class="fr-warn" title="the reported position does not match this route">??</span>';
    const track = '<div class="fr-track"><i class="fr-base"></i>'
      + (pc != null ? `<i class="fr-done" style="width:${pc}%"></i><span class="fr-plane" style="left:${pc}%">${PLANE_SVG}</span>` : '')
      + '<i class="fr-dot o"></i><i class="fr-dot d"></i></div>';
    // middle slot: stops for a multi-leg route, else the live ETA
    let mid = '';
    if (ap.length > 2) mid = 'via ' + ap.slice(1, -1).map(code).map(esc).join(' · ');
    else if (p && p.eta) mid = 'lands ≈ ' + new Date(p.eta).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    return `<div class="fr-leg">`
      + `<div class="fr-row">${warn}<span class="fr-code">${esc(code(org))}</span>${track}`
      + `<span class="fr-code fr-dest" title="destination — where it lands">${esc(code(dst))}</span></div>`
      + `<div class="fr-cities"><span class="fr-o">${esc(place(org))}</span>`
      + (mid ? `<span class="fr-m">${mid}</span>` : '')
      + `<span class="fr-d">${esc(place(dst))}</span></div>`
      + `</div>`;
  }

  global.FlightRoute = { get, lookup, onUpdate: cb => cbs.push(cb), dest, legHTML, shortLabel, code, place, enabled: !off };
})(window);
