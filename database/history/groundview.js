/* Shared "ground view": draw one or more aircraft as they would look from a point on the ground.
 *
 * Each trail fix becomes a line-of-sight (azimuth, elevation). Points are placed with a true
 * pinhole/perspective (gnomonic) camera aimed at the action, so a loop looks like a loop and the
 * formation keeps its real shape. The sky is tinted from the Sun's position at the flight's own
 * time (day / golden hour / night), with the Sun (or its glow) drawn where it actually is.
 *
 * Used by the History page (replay across a time scrubber) and the Live page (pinned to "now").
 * Pages provide a panel with the fixed ids below and call open()/setObserver()/updateTracks().
 *   #groundview  #gv-canvas  #gv-readout  #gv-from  #gv-scrub  #gv-play  #gv-close  #gv-title
 */
(function (global) {
  'use strict';
  const $ = id => document.getElementById(id);
  const D2R = Math.PI / 180, R2D = 180 / Math.PI;

  function unit(azDeg, elDeg) {              // line-of-sight unit vector (x east, y north, z up)
    const a = azDeg * D2R, e = elDeg * D2R, ce = Math.cos(e);
    return [Math.sin(a) * ce, Math.cos(a) * ce, Math.sin(e)];
  }
  const cross = (a, b) => [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]];
  const dot = (a, b) => a[0]*b[0] + a[1]*b[1] + a[2]*b[2];
  const norm = a => { const m = Math.hypot(a[0], a[1], a[2]) || 1; return [a[0]/m, a[1]/m, a[2]/m]; };
  function haversineM(la1, lo1, la2, lo2) {
    const R = 6371000, dla = (la2-la1)*D2R, dlo = (lo2-lo1)*D2R;
    const x = Math.sin(dla/2)**2 + Math.cos(la1*D2R)*Math.cos(la2*D2R)*Math.sin(dlo/2)**2;
    return 2 * R * Math.asin(Math.sqrt(x));
  }
  function bearing(la1, lo1, la2, lo2) {
    const dl = (lo2-lo1)*D2R, y = Math.sin(dl)*Math.cos(la2*D2R);
    const x = Math.cos(la1*D2R)*Math.sin(la2*D2R) - Math.sin(la1*D2R)*Math.cos(la2*D2R)*Math.cos(dl);
    return (Math.atan2(y, x) * R2D + 360) % 360;
  }
  function compass(az) { az = ((az % 360) + 360) % 360;
    return Math.round(az) + '° ' + ['N','NE','E','SE','S','SW','W','NW'][Math.round(az/45)%8]; }

  // Low-precision solar position (good enough for tint + drawing the Sun). Returns {az, el} deg.
  function sunPosition(ms, lat, lon) {
    const n = ms/86400000 + 2440587.5 - 2451545.0;      // days since J2000
    const L = (280.460 + 0.9856474*n) * D2R;
    const g = (357.528 + 0.9856003*n) * D2R;
    const lambda = L + (1.915*Math.sin(g) + 0.020*Math.sin(2*g)) * D2R;
    const eps = 23.439 * D2R;
    const ra = Math.atan2(Math.cos(eps)*Math.sin(lambda), Math.cos(lambda));
    const dec = Math.asin(Math.sin(eps)*Math.sin(lambda));
    const gmst = (18.697374558 + 24.06570982441908*n) % 24;
    const lst = (gmst*15 + lon) * D2R;
    const ha = lst - ra, latr = lat*D2R;
    const el = Math.asin(Math.sin(latr)*Math.sin(dec) + Math.cos(latr)*Math.cos(dec)*Math.cos(ha));
    let az = Math.atan2(-Math.sin(ha), Math.tan(dec)*Math.cos(latr) - Math.sin(latr)*Math.cos(ha));
    return { az: ((az*R2D) + 360) % 360, el: el*R2D };
  }
  const lerp = (a, b, t) => a + (b - a) * t;
  function mix(c1, c2, t) { return [Math.round(lerp(c1[0],c2[0],t)), Math.round(lerp(c1[1],c2[1],t)), Math.round(lerp(c1[2],c2[2],t))]; }
  const rgb = c => `rgb(${c[0]},${c[1]},${c[2]})`;
  // sky top / horizon colours blended by Sun elevation (night -> twilight -> day)
  function skyColours(sunEl) {
    const NIGHT_T=[6,9,16], NIGHT_H=[14,22,38], DUSK_T=[20,32,58], DUSK_H=[120,70,52], DAY_T=[28,64,112], DAY_H=[120,160,205];
    if (sunEl >= 6)  return { top: DAY_T,  hor: DAY_H,  night: false };
    if (sunEl >= -6) { const t = (sunEl + 6) / 12; return { top: mix(DUSK_T,DAY_T,t), hor: mix(DUSK_H,DAY_H,t), night: false }; }
    if (sunEl >= -12){ const t = (sunEl + 12) / 6; return { top: mix(NIGHT_T,DUSK_T,t), hor: mix(NIGHT_H,DUSK_H,t), night: t < 0.5 }; }
    return { top: NIGHT_T, hor: NIGHT_H, night: true };
  }

  class GroundView {
    constructor(o) {
      o = o || {};
      this.altColor = o.altColor || (() => '#9bd');
      this.colorFor = o.colorFor || (() => '#6cc1ff');
      this.onTime = o.onTime || (() => {});       // (tms, positions[]) -> page moves map markers
      this.onClose = o.onClose || (() => {});
      this.observer = null; this.tracks = []; this.t = 0; this.tmin = 0; this.tmax = 0;
      this.cam = null; this.playing = false; this.timer = null; this.live = false; this._open = false;
      this._wire();
    }
    _wire() {
      const sc = $('gv-scrub'); if (sc) sc.oninput = () => { this.pause(); this.setTime(+sc.value); };
      const pl = $('gv-play'); if (pl) pl.onclick = () => this.playing ? this.pause() : this.play();
      const cl = $('gv-close'); if (cl) cl.onclick = () => this.close();
      window.addEventListener('resize', () => { if (this._open) this.draw(); });
    }
    open(tracks, observer, opts) {
      opts = opts || {}; this.live = !!opts.live;
      if (observer) this.observer = observer;
      this.setTracks(tracks);
      this._open = true; $('groundview').classList.add('on');
      const sc = $('gv-scrub'), pl = $('gv-play');
      if (sc) sc.style.display = this.live ? 'none' : '';
      if (pl) pl.style.display = this.live ? 'none' : '';
      this.projectAll(); this.fitView(); this.setTime(this.tmax);
    }
    close() { this._open = false; this.pause(); $('groundview').classList.remove('on'); this.onClose(); }
    isOpen() { return this._open; }
    setObserver(lat, lon) { this.observer = { lat, lon }; this.projectAll(); this.fitView(); this.draw(); }
    setTracks(tracks) {
      this.tracks = (tracks || []).filter(t => t.points && t.points.length).map(t => Object.assign({ sky: null }, t));
      let lo = Infinity, hi = -Infinity;
      for (const t of this.tracks) for (const p of t.points) { if (p[0] < lo) lo = p[0]; if (p[0] > hi) hi = p[0]; }
      this.tmin = lo; this.tmax = hi;
      const sc = $('gv-scrub');
      if (sc && isFinite(lo)) { sc.min = lo; sc.max = hi; sc.step = Math.max(1, (hi - lo) / 800); }
      if (isFinite(hi)) this.t = hi;
    }
    // live: refresh points/cursor without re-framing the camera each tick (less jitter)
    updateTracks(tracks) {
      const had = !!this.cam;
      this.setTracks(tracks); this.projectAll();
      if (!had) this.fitView(); else this.fitView();   // re-fit but keep it cheap; formation drifts slowly
      this.setTime(this.tmax);
    }
    projectAll() { if (this.observer) for (const t of this.tracks) t.sky = this._sky(t.points); }
    _sky(points) {
      const o = this.observer, out = []; let prev = null, off = 0;
      for (const p of points) {
        if (p[1] == null || p[2] == null) continue;
        const d = haversineM(o.lat, o.lon, p[1], p[2]);
        let az = bearing(o.lat, o.lon, p[1], p[2]) + off;
        if (prev != null) { while (az - prev > 180) { az -= 360; off -= 360; } while (az - prev < -180) { az += 360; off += 360; } }
        prev = az;
        const h = (p[3] == null ? 0 : Math.max(0, p[3])) * 0.3048;
        const el = Math.atan2(h, Math.max(d, 1)) * R2D;
        out.push({ t: p[0], az, el, dist: Math.sqrt(d*d + h*h), alt: p[3] });
      }
      return out;
    }
    fitView() {
      const all = []; for (const t of this.tracks) if (t.sky) all.push(...t.sky);
      if (!all.length) { this.cam = null; return; }
      let vx = 0, vy = 0, vz = 0;
      for (const s of all) { const u = unit(s.az, s.el); vx += u[0]; vy += u[1]; vz += u[2]; }
      const f = norm([vx, vy, vz]);
      let right = cross([0, 0, 1], f); if (Math.hypot(right[0], right[1], right[2]) < 1e-3) right = [1, 0, 0];
      right = norm(right); const up = cross(f, right);
      let mx = 0.15, my = 0.15;
      for (const s of all) { const u = unit(s.az, s.el), fwd = dot(u, f); if (fwd <= 0.05) continue;
        mx = Math.max(mx, Math.abs(dot(u, right) / fwd)); my = Math.max(my, Math.abs(dot(u, up) / fwd)); }
      this.cam = { f, right, up, mx, my };
    }
    _project(az, el, w, h) {
      const c = this.cam; if (!c) return null;
      const u = unit(az, el), fwd = dot(u, c.f); if (fwd <= 0.02) return null;
      const pad = 28, focal = Math.min((w/2 - pad) / c.mx, (h/2 - pad) / c.my);
      return [w/2 + (dot(u, c.right)/fwd)*focal, h/2 - (dot(u, c.up)/fwd)*focal, focal, fwd];
    }
    _sampleSky(track, t) {
      const s = track.sky; if (!s || !s.length) return null;
      if (t < s[0].t - 3000 || t > s[s.length-1].t + 3000) return null;
      let i = 0; while (i < s.length-1 && s[i+1].t <= t) i++;
      const a = s[i], b = s[Math.min(s.length-1, i+1)]; if (b.t === a.t) return a;
      const f = Math.max(0, Math.min(1, (t - a.t) / (b.t - a.t)));
      return { az: a.az+(b.az-a.az)*f, el: a.el+(b.el-a.el)*f, dist: a.dist+(b.dist-a.dist)*f,
               alt: (a.alt!=null && b.alt!=null) ? a.alt+(b.alt-a.alt)*f : a.alt };
    }
    _sampleRaw(track, t) {
      const p = track.points; if (!p.length) return null;
      if (t < p[0][0] - 3000 || t > p[p.length-1][0] + 3000) return null;
      let i = 0; while (i < p.length-1 && p[i+1][0] <= t) i++;
      const a = p[i], b = p[Math.min(p.length-1, i+1)]; if (b[0] === a[0]) return { lat:a[1], lon:a[2], alt:a[3] };
      const f = Math.max(0, Math.min(1, (t - a[0]) / (b[0] - a[0])));
      return { lat: a[1]+(b[1]-a[1])*f, lon: a[2]+(b[2]-a[2])*f, alt: (a[3]!=null&&b[3]!=null)?a[3]+(b[3]-a[3])*f:a[3] };
    }
    setTime(t) {
      this.t = t; const sc = $('gv-scrub'); if (sc && !this.live) sc.value = t;
      this.draw();
      const positions = [];
      for (const tr of this.tracks) { const r = this._sampleRaw(tr, t); if (r) positions.push(Object.assign({ id: tr.id, color: tr.color, label: tr.label }, r)); }
      this.onTime(t, positions);
    }
    play() {
      if (this.live) return; this.playing = true; const pl = $('gv-play'); if (pl) pl.textContent = '❚❚';
      const span = this.tmax - this.tmin || 1; if (this.t >= this.tmax) this.t = this.tmin;
      this.timer = setInterval(() => { this.t += span / 240; if (this.t >= this.tmax) { this.t = this.tmax; this.pause(); } this.setTime(this.t); }, 55);
    }
    pause() { this.playing = false; if (this.timer) { clearInterval(this.timer); this.timer = null; } const pl = $('gv-play'); if (pl) pl.textContent = '▶'; }

    draw() {
      const c = $('gv-canvas'); if (!c) return;
      const r = c.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
      c.width = Math.max(1, r.width*dpr); c.height = Math.max(1, r.height*dpr);
      const ctx = c.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const w = r.width, h = r.height;
      // Sun + sky tint at the current time
      const sun = this.observer ? sunPosition(this.t, this.observer.lat, this.observer.lon) : { az: 180, el: 30 };
      const sky = skyColours(sun.el);
      const grad = ctx.createLinearGradient(0, 0, 0, h);
      grad.addColorStop(0, rgb(sky.top)); grad.addColorStop(1, rgb(sky.hor));
      ctx.fillStyle = grad; ctx.fillRect(0, 0, w, h);
      if (!this.cam || !this.observer) { ctx.fillStyle = '#6b7480'; ctx.font = '12px system-ui';
        ctx.fillText('Select a flight to watch it from the ground.', 16, h/2); return; }
      // Sun glow + disk
      const sp = this._project(sun.az, sun.el, w, h);
      if (sp && sun.el > -10) {
        const gl = ctx.createRadialGradient(sp[0], sp[1], 0, sp[0], sp[1], Math.max(w,h)*0.5);
        const warm = sun.el > 2 ? 'rgba(255,240,200,.18)' : 'rgba(255,160,90,.22)';
        gl.addColorStop(0, warm); gl.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.fillStyle = gl; ctx.fillRect(0, 0, w, h);
        if (sun.el > -1) { ctx.beginPath(); ctx.arc(sp[0], sp[1], sun.el > 2 ? 9 : 11, 0, 7);
          ctx.fillStyle = sun.el > 2 ? '#fff6da' : '#ffba78'; ctx.fill(); }
      }
      // elevation gridlines + horizon, by projecting el lines across the view's azimuth span
      const c0 = this.cam, fwdAz = (Math.atan2(c0.f[0], c0.f[1]) * R2D + 360) % 360;
      ctx.font = '10px ui-monospace, monospace';
      function elLine(self, el, draw) {
        let prev = null; const pts2 = [];
        for (let a = -100; a <= 100; a += 4) { const p = self._project(fwdAz + a, el, w, h); pts2.push(p); }
        ctx.beginPath(); let started = false;
        for (const p of pts2) { if (!p) { started = false; continue; } if (!started) { ctx.moveTo(p[0], p[1]); started = true; } else ctx.lineTo(p[0], p[1]); }
        ctx.stroke();
      }
      for (const e of [0, 15, 30, 45, 60, 75]) {
        ctx.strokeStyle = e === 0 ? 'rgba(160,185,225,.6)' : 'rgba(255,255,255,.08)';
        ctx.lineWidth = e === 0 ? 1.6 : 1; elLine(this, e);
        const lp = this._project(fwdAz, e, w, h);
        if (lp) { ctx.fillStyle = e === 0 ? '#aebfdc' : '#5b6678'; ctx.fillText(e === 0 ? 'horizon' : e + '°', 4, lp[1] - 4); }
      }
      // a soft "ground" wash below the horizon
      const hz = this._project(fwdAz, 0, w, h);
      if (hz) { const gg = ctx.createLinearGradient(0, hz[1], 0, h); gg.addColorStop(0, 'rgba(10,14,20,.0)'); gg.addColorStop(1, 'rgba(6,9,14,.55)');
        ctx.fillStyle = gg; ctx.fillRect(0, Math.max(0, hz[1]), w, h); }
      // azimuth (compass) ticks along the bottom
      ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
      for (let a = 0; a < 360; a += 15) {
        const p = this._project(a, Math.max(1, sky.night ? 1 : 1), w, h); if (!p || p[3] < 0.1) continue;
        if (p[0] < 6 || p[0] > w - 6) continue;
        ctx.fillStyle = (a % 90 === 0) ? '#9fb0cc' : '#6a7488'; ctx.fillText(compass(a), p[0], h - 5);
      }
      ctx.textAlign = 'start';
      const multi = this.tracks.length > 1;
      // each track's trajectory + current aircraft
      let readout = '';
      for (const tr of this.tracks) {
        if (!tr.sky || tr.sky.length < 2) continue;
        // trajectory
        ctx.lineWidth = 2.4; ctx.lineJoin = 'round';
        for (let i = 1; i < tr.sky.length; i++) {
          const p1 = this._project(tr.sky[i-1].az, tr.sky[i-1].el, w, h), p2 = this._project(tr.sky[i].az, tr.sky[i].el, w, h);
          if (!p1 || !p2) continue;
          ctx.strokeStyle = multi ? tr.color : this.altColor(tr.sky[i].alt);
          ctx.globalAlpha = multi ? 0.5 : 0.85;
          ctx.beginPath(); ctx.moveTo(p1[0], p1[1]); ctx.lineTo(p2[0], p2[1]); ctx.stroke();
        }
        ctx.globalAlpha = 1;
        // aircraft at current time
        const s = this._sampleSky(tr, this.t); if (!s) continue;
        const p = this._project(s.az, s.el, w, h); if (!p) continue;
        // drop line to the horizon (depth cue)
        const hp = this._project(s.az, 0, w, h);
        if (hp) { ctx.strokeStyle = 'rgba(255,255,255,.16)'; ctx.setLineDash([3,3]); ctx.lineWidth = 1;
          ctx.beginPath(); ctx.moveTo(p[0], hp[1]); ctx.lineTo(p[0], p[1]); ctx.stroke(); ctx.setLineDash([]); }
        // apparent angular size of a ~12 m wingspan at this slant range
        const sz = Math.max(5, Math.min(46, (12 / Math.max(s.dist, 50)) * p[2]));
        const prev = this._sampleSky(tr, this.t - 1500) || s;
        const pp = this._project(prev.az, prev.el, w, h) || p;
        const rot = Math.atan2(p[1]-pp[1], p[0]-pp[0]);
        ctx.save(); ctx.translate(p[0], p[1]); ctx.rotate(rot);
        ctx.fillStyle = this.altColor(s.alt); ctx.strokeStyle = multi ? tr.color : 'rgba(0,0,0,.7)'; ctx.lineWidth = multi ? 2 : 1;
        ctx.beginPath(); ctx.moveTo(sz*0.95,0); ctx.lineTo(-sz*0.6, sz*0.5); ctx.lineTo(-sz*0.22,0); ctx.lineTo(-sz*0.6,-sz*0.5); ctx.closePath();
        ctx.fill(); ctx.stroke(); ctx.restore();
        // label
        if (tr.label) { ctx.font = '11px system-ui'; ctx.textAlign = 'center'; ctx.fillStyle = multi ? tr.color : '#dfe6f0';
          ctx.fillText(tr.label, p[0], p[1] - sz*0.6 - 4); ctx.textAlign = 'start'; }
        if (!readout) readout = `bearing <b>${compass(s.az)}</b> · elevation <b>${s.el.toFixed(0)}°</b>`
          + ` · range <b>${s.dist>=1000 ? (s.dist/1000).toFixed(1)+' km' : Math.round(s.dist)+' m'}</b>`
          + ` · alt <b>${s.alt!=null ? Math.round(s.alt).toLocaleString()+' ft' : '—'}</b>`;
      }
      const ro = $('gv-readout');
      if (ro) {
        const when = new Date(this.t);
        const sunTxt = sun.el > 0 ? `☀ ${sun.el.toFixed(0)}°` : sun.el > -6 ? '🌆 dusk' : '🌙 night';
        ro.innerHTML = (readout || 'no aircraft up at this moment') + ` &nbsp;·&nbsp; ${sunTxt}`
          + (this.live ? '' : ` &nbsp;·&nbsp; ${when.toLocaleTimeString()}`);
      }
    }
  }
  global.GroundView = GroundView;
  global.GroundViewUtil = { sunPosition, compass };
})(window);
