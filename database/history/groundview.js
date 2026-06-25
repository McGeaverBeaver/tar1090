/* Shared 3D "ground view": stand at a point on the ground and watch the aircraft fly overhead,
 * rendered as real 3D models over a satellite-textured ground, lit by the Sun for the time of day.
 * Orbit/zoom with mouse drag + scroll (pinch on touch). WebGL via Three.js.
 *
 * Public API (kept stable so the History + Live pages need almost no change):
 *   new GroundView({altColor, colorFor, onTime, onClose})
 *   open(tracks, observer, {live})   close()   isOpen()
 *   setObserver(lat,lon)   setTracks(tracks)   projectAll()   fitView()   setTime(tms)
 *   updateTracks(tracks)             // live: replace points each tick, cursor pinned to "now"
 * tracks: [{ id, label, color, points:[[tms,lat,lon,altFt],...] }]   observer: {lat,lon}
 *
 * Panel ids it drives: #groundview #gv-canvas #gv-readout #gv-from #gv-scrub #gv-play #gv-close
 */
(function (global) {
  'use strict';
  const $ = id => document.getElementById(id);
  const D2R = Math.PI / 180, R2D = 180 / Math.PI, FT2M = 0.3048;

  function haversineM(la1, lo1, la2, lo2) {
    const R = 6371000, dla = (la2-la1)*D2R, dlo = (lo2-lo1)*D2R;
    const a = Math.sin(dla/2)**2 + Math.cos(la1*D2R)*Math.cos(la2*D2R)*Math.sin(dlo/2)**2;
    return 2 * R * Math.asin(Math.sqrt(a));
  }
  function bearing(la1, lo1, la2, lo2) {
    const dl = (lo2-lo1)*D2R, y = Math.sin(dl)*Math.cos(la2*D2R);
    const x = Math.cos(la1*D2R)*Math.sin(la2*D2R) - Math.sin(la1*D2R)*Math.cos(la2*D2R)*Math.cos(dl);
    return (Math.atan2(y, x) * R2D + 360) % 360;
  }
  function compass(az) { az = ((az % 360) + 360) % 360;
    return Math.round(az) + '° ' + ['N','NE','E','SE','S','SW','W','NW'][Math.round(az/45)%8]; }
  // east/north/up metres of an aircraft relative to the observer (scene: x=east, y=up, z=-north)
  function enu(o, lat, lon, altFt) {
    const d = haversineM(o.lat, o.lon, lat, lon), az = bearing(o.lat, o.lon, lat, lon);
    const up = (altFt == null ? 0 : Math.max(0, altFt)) * FT2M;
    return { x: d * Math.sin(az*D2R), y: up, z: -d * Math.cos(az*D2R), d, az, up, alt: altFt };
  }
  function sunPosition(ms, lat, lon) {
    const n = ms/86400000 + 2440587.5 - 2451545.0;
    const L = (280.460 + 0.9856474*n) * D2R, g = (357.528 + 0.9856003*n) * D2R;
    const lambda = L + (1.915*Math.sin(g) + 0.020*Math.sin(2*g)) * D2R, eps = 23.439 * D2R;
    const ra = Math.atan2(Math.cos(eps)*Math.sin(lambda), Math.cos(lambda));
    const dec = Math.asin(Math.sin(eps)*Math.sin(lambda));
    const gmst = (18.697374558 + 24.06570982441908*n) % 24, lst = (gmst*15 + lon) * D2R;
    const ha = lst - ra, latr = lat*D2R;
    const el = Math.asin(Math.sin(latr)*Math.sin(dec) + Math.cos(latr)*Math.cos(dec)*Math.cos(ha));
    let az = Math.atan2(-Math.sin(ha), Math.tan(dec)*Math.cos(latr) - Math.sin(latr)*Math.cos(ha));
    return { az: ((az*R2D)+360)%360, el: el*R2D };
  }
  const lerp = (a,b,t) => a + (b-a)*t, mix = (c1,c2,t) => [Math.round(lerp(c1[0],c2[0],t)),Math.round(lerp(c1[1],c2[1],t)),Math.round(lerp(c1[2],c2[2],t))];
  const rgb = c => `rgb(${c[0]},${c[1]},${c[2]})`;
  function skyColours(sunEl) {
    const N_T=[6,9,16],N_H=[14,22,38],D_T=[20,32,58],D_H=[150,86,58],Y_T=[40,92,150],Y_H=[150,186,224];
    if (sunEl >= 8)  return { top:Y_T, hor:Y_H, light:1.0, night:false };
    if (sunEl >= -6) { const t=(sunEl+6)/14; return { top:mix(D_T,Y_T,t), hor:mix(D_H,Y_H,t), light:0.25+0.75*t, night:false }; }
    if (sunEl >= -14){ const t=(sunEl+14)/8; return { top:mix(N_T,D_T,t), hor:mix(N_H,D_H,t), light:0.06+0.19*t, night:t<0.5 }; }
    return { top:N_T, hor:N_H, light:0.06, night:true };
  }

  const ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export';

  class GroundView {
    constructor(o) {
      o = o || {};
      this.altColor = o.altColor || (() => '#9bd');
      this.onTime = o.onTime || (() => {});
      this.onClose = o.onClose || (() => {});
      this.observer = null; this.tracks = []; this.t = 0; this.tmin = 0; this.tmax = 0;
      this.live = false; this._open = false; this.three = null;
      this.mode = 'fly';                         // 'fly' = orbit the action, 'stand' = FPV in place
      this.fpv = { yaw: 0, pitch: 18, fov: 58 };
      this._wire();
    }
    _wire() {
      const sc = $('gv-scrub'); if (sc) sc.oninput = () => { this.pause(); this.setTime(+sc.value); };
      const pl = $('gv-play'); if (pl) pl.onclick = () => this.playing ? this.pause() : this.play();
      const cl = $('gv-close'); if (cl) cl.onclick = () => this.close();
      const md = $('gv-mode'); if (md) md.onclick = () => this.setMode(this.mode === 'fly' ? 'stand' : 'fly');
      window.addEventListener('resize', () => this._resize());
    }

    // ---- public API -------------------------------------------------------
    open(tracks, observer, opts) {
      opts = opts || {}; this.live = !!opts.live;
      if (observer) this.observer = { lat: observer.lat, lon: observer.lon };
      this._open = true; $('groundview').classList.add('on');
      const sc = $('gv-scrub'), pl = $('gv-play');
      if (sc) sc.style.display = this.live ? 'none' : '';
      if (pl) pl.style.display = this.live ? 'none' : '';
      if (!this._ensure()) { this._fallback(); return; }
      this.setTracks(tracks); this._loadGround(); this.projectAll(); this.fitView(); this.setTime(this.tmax);
      this._resize(); this._start();
    }
    close() {
      this._open = false; this.pause(); this._stop();
      $('groundview').classList.remove('on'); this.onClose();
    }
    isOpen() { return this._open; }
    setObserver(lat, lon) { this.observer = { lat, lon }; if (!this.three) return; this._loadGround(); this.projectAll(); this.fitView(); this.setTime(this.t); }
    setTracks(tracks) {
      this.tracks = (tracks || []).filter(t => t.points && t.points.length).map(t => Object.assign({}, t));
      let lo = Infinity, hi = -Infinity;
      for (const t of this.tracks) for (const p of t.points) { if (p[0] < lo) lo = p[0]; if (p[0] > hi) hi = p[0]; }
      this.tmin = lo; this.tmax = hi;
      const sc = $('gv-scrub'); if (sc && isFinite(lo)) { sc.min = lo; sc.max = hi; sc.step = Math.max(1, (hi-lo)/800); }
      if (isFinite(hi)) this.t = hi;
    }
    updateTracks(tracks) { if (!this.three) return; this.setTracks(tracks); this.projectAll(); this.setTime(this.tmax); }

    // ---- three.js scene ---------------------------------------------------
    _ensure() {
      if (this.three) return true;
      if (!global.THREE || !global.THREE.WebGLRenderer || !global.THREE.OrbitControls) return false;
      const T = global.THREE, canvas = $('gv-canvas');
      let renderer;
      try { renderer = new T.WebGLRenderer({ canvas, antialias: true }); } catch (e) { return false; }
      renderer.setPixelRatio(Math.min(2, global.devicePixelRatio || 1));
      const scene = new T.Scene();
      const camera = new T.PerspectiveCamera(58, 1, 1, 600000);
      camera.position.set(0, 2, 0.1);
      const controls = new T.OrbitControls(camera, renderer.domElement);
      controls.enableDamping = true; controls.dampingFactor = 0.08;
      controls.minDistance = 4; controls.maxDistance = 300000; controls.zoomSpeed = 1.2;
      // first-person ("stand") controls: look around in place by dragging, zoom = field of view
      const dom = renderer.domElement, ptrs = new Map(); let pinch = 0;
      const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
      dom.addEventListener('pointerdown', e => { if (this.mode !== 'stand') return; ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY }); dom.setPointerCapture(e.pointerId); });
      dom.addEventListener('pointermove', e => {
        if (this.mode !== 'stand' || !ptrs.has(e.pointerId)) return;
        const p = ptrs.get(e.pointerId);
        if (ptrs.size >= 2) {                              // pinch -> field of view
          const a = [...ptrs.values()]; const d = Math.hypot(a[0].x - a[1].x, a[0].y - a[1].y);
          if (pinch) { this.fpv.fov = clamp(this.fpv.fov * (pinch / d), 20, 85); camera.fov = this.fpv.fov; camera.updateProjectionMatrix(); }
          pinch = d;
        } else {                                           // drag -> look around
          this.fpv.yaw -= (e.clientX - p.x) * 0.22; this.fpv.pitch = clamp(this.fpv.pitch + (e.clientY - p.y) * 0.22, -8, 88); this._applyFpv();
        }
        p.x = e.clientX; p.y = e.clientY;
      });
      const drop = e => { ptrs.delete(e.pointerId); if (ptrs.size < 2) pinch = 0; };
      dom.addEventListener('pointerup', drop); dom.addEventListener('pointercancel', drop);
      dom.addEventListener('wheel', e => { if (this.mode !== 'stand') return; e.preventDefault();
        this.fpv.fov = clamp(this.fpv.fov + Math.sign(e.deltaY) * 3, 20, 85); camera.fov = this.fpv.fov; camera.updateProjectionMatrix(); }, { passive: false });
      // sky dome
      const skyCanvas = document.createElement('canvas'); skyCanvas.width = 8; skyCanvas.height = 256;
      const skyTex = new T.CanvasTexture(skyCanvas);
      const sky = new T.Mesh(new T.SphereGeometry(400000, 24, 12),
        new T.MeshBasicMaterial({ map: skyTex, side: T.BackSide, depthWrite: false, fog: false }));
      scene.add(sky);
      // lights
      const hemi = new T.HemisphereLight(0xbfd4ff, 0x202830, 0.7); scene.add(hemi);
      const sun = new T.DirectionalLight(0xffffff, 1.0); scene.add(sun);
      const sunBall = new T.Mesh(new T.SphereGeometry(4000, 16, 12),
        new T.MeshBasicMaterial({ color: 0xfff3c0, fog: false })); scene.add(sunBall);
      // ground
      const groundMat = new T.MeshStandardMaterial({ color: 0x2a3340, roughness: 1, metalness: 0 });
      const ground = new T.Mesh(new T.PlaneGeometry(1, 1), groundMat);
      ground.rotation.x = -Math.PI / 2; scene.add(ground);
      // a faint reference grid on top of the ground
      const grid = new T.GridHelper(20000, 40, 0x4a5a72, 0x2c3748); grid.position.y = 0.5;
      grid.material.transparent = true; grid.material.opacity = 0.25; scene.add(grid);
      const fleet = new T.Group(); scene.add(fleet);
      scene.fog = new T.Fog(0x9fb4d8, 2000, 60000);
      this.three = { T, renderer, scene, camera, controls, sky, skyCanvas, skyTex, hemi, sun, sunBall, ground, groundMat, grid, fleet, raf: 0, meshes: {} };
      return true;
    }
    _planeMesh(color, spanM) {
      // Object3D.lookAt() aims the local +Z at the target, so the nose points +Z (tail at -Z).
      const T = this.three.T, g = new T.Group();
      const mat = new T.MeshStandardMaterial({ color: new T.Color(color), roughness: 0.55, metalness: 0.15 });
      const body = new T.Mesh(new T.CylinderGeometry(spanM*0.02, spanM*0.05, spanM*0.95, 10), mat);
      body.rotation.x = Math.PI / 2;                         // pointy end (+Z) is the nose
      const wing = new T.Mesh(new T.BoxGeometry(spanM, spanM*0.04, spanM*0.16), mat); wing.position.z = -spanM*0.02;
      const tail = new T.Mesh(new T.BoxGeometry(spanM*0.4, spanM*0.04, spanM*0.1), mat); tail.position.z = -spanM*0.42;
      const fin = new T.Mesh(new T.BoxGeometry(spanM*0.04, spanM*0.22, spanM*0.12), mat); fin.position.set(0, spanM*0.09, -spanM*0.42);
      g.add(body, wing, tail, fin); return g;
    }
    projectAll() {
      if (!this.three || !this.observer) return;
      const T = this.three.T, fleet = this.three.fleet;
      // dispose the previous fleet's GPU resources (this runs every live tick)
      fleet.traverse(o => { if (o.geometry) o.geometry.dispose();
        if (o.material) { if (o.material.map) o.material.map.dispose(); o.material.dispose(); } });
      while (fleet.children.length) fleet.remove(fleet.children[0]);
      this.three.meshes = {};
      const EXAG = 22, span = 12 * EXAG;                     // exaggerate ~12 m wingspan so the model is visible
      for (const tr of this.tracks) {
        const col = tr.color || '#6cc1ff';
        // trail (decimated)
        const stepN = Math.max(1, Math.floor(tr.points.length / 400));
        const verts = [];
        for (let i = 0; i < tr.points.length; i += stepN) { const p = tr.points[i]; if (p[1] == null) continue; const e = enu(this.observer, p[1], p[2], p[3]); verts.push(e.x, e.y, e.z); }
        const lineGeo = new T.BufferGeometry(); lineGeo.setAttribute('position', new T.Float32BufferAttribute(verts, 3));
        const line = new T.Line(lineGeo, new T.LineBasicMaterial({ color: new T.Color(col), transparent: true, opacity: 0.7 }));
        fleet.add(line);
        const plane = this._planeMesh(col, span); fleet.add(plane);
        const label = this._sprite(tr.label || '', col); if (label) fleet.add(label);
        this.three.meshes[tr.id] = { plane, label, track: tr };
      }
    }
    _sprite(text, color) {
      if (!text) return null;
      const T = this.three.T, c = document.createElement('canvas'), pad = 8;
      const ctx = c.getContext('2d'); ctx.font = 'bold 40px system-ui';
      c.width = ctx.measureText(text).width + pad*2; c.height = 54;
      const g = ctx; g.font = 'bold 40px system-ui'; g.fillStyle = 'rgba(8,10,14,.65)';
      g.fillRect(0, 0, c.width, c.height); g.fillStyle = color; g.textBaseline = 'middle'; g.fillText(text, pad, c.height/2);
      const tex = new T.CanvasTexture(c);
      const sp = new T.Sprite(new T.SpriteMaterial({ map: tex, depthTest: false, depthWrite: false }));
      sp.scale.set(c.width/c.height * 1400, 1400, 1); sp.userData.aspect = c.width/c.height; return sp;
    }
    _centroid() {
      let n = 0, cx = 0, cy = 0, cz = 0;
      for (const tr of this.tracks) for (const p of tr.points) { if (p[1] == null) continue; const e = enu(this.observer, p[1], p[2], p[3]); cx += e.x; cy += e.y; cz += e.z; n++; }
      return n ? { x: cx/n, y: cy/n, z: cz/n, n } : { x: 0, y: 500, z: -2000, n: 0 };
    }
    fitView() {
      if (!this.three) return;
      if (this.mode === 'stand') { this._standAim(); return; }
      const c = this.three.controls, m = this._centroid();
      c.target.set(m.x, m.y, m.z);                          // orbit around the action; scroll zooms toward it
      this.three.camera.position.set(m.x * 0.02, Math.max(2, m.y * 0.15), m.z * 0.02 + Math.sign(m.z || -1) * 0.1);
      c.enabled = true; c.update();
    }
    _standAim() {                                            // point the FPV camera at the action centroid
      const m = this._centroid(), d = Math.hypot(m.x, m.z);
      this.fpv.yaw = (Math.atan2(m.x, -m.z) * R2D);          // azimuth toward the action
      this.fpv.pitch = Math.max(0, Math.min(85, Math.atan2(m.y, Math.max(d, 1)) * R2D));
      this._applyFpv();
    }
    _applyFpv() {
      if (!this.three) return;
      const cam = this.three.camera, y = this.fpv.yaw * D2R, p = this.fpv.pitch * D2R;
      const dir = new this.three.T.Vector3(Math.sin(y) * Math.cos(p), Math.sin(p), -Math.cos(y) * Math.cos(p));
      cam.position.set(0, 2, 0);
      cam.lookAt(dir.x * 1000, 2 + dir.y * 1000, dir.z * 1000);
      cam.fov = this.fpv.fov; cam.updateProjectionMatrix();
    }
    setMode(m) {
      this.mode = (m === 'stand') ? 'stand' : 'fly';
      const md = $('gv-mode'); if (md) md.textContent = this.mode === 'stand' ? '🧍 Stand' : '🎥 Fly';
      if (!this.three) return;
      if (this.mode === 'stand') { this.three.controls.enabled = false; this._standAim(); }
      else { this.three.camera.fov = 58; this.three.camera.updateProjectionMatrix(); this.fitView(); }
    }
    _sampleRaw(track, t) {
      const p = track.points; if (!p.length) return null;
      if (t < p[0][0] - 4000 || t > p[p.length-1][0] + 4000) return null;
      let i = 0; while (i < p.length-1 && p[i+1][0] <= t) i++;
      const a = p[i], b = p[Math.min(p.length-1, i+1)]; if (b[0] === a[0]) return { lat:a[1], lon:a[2], alt:a[3], prev:a };
      const f = Math.max(0, Math.min(1, (t-a[0])/(b[0]-a[0])));
      return { lat:a[1]+(b[1]-a[1])*f, lon:a[2]+(b[2]-a[2])*f, alt:(a[3]!=null&&b[3]!=null)?a[3]+(b[3]-a[3])*f:a[3], prev:a };
    }
    setTime(t) {
      this.t = t; const sc = $('gv-scrub'); if (sc && !this.live) sc.value = t;
      if (!this.three || !this.observer) return;
      const T = this.three.T;
      // sun + sky + light
      const sun = sunPosition(t, this.observer.lat, this.observer.lon), sky = skyColours(sun.el);
      const g = this.three.skyCanvas.getContext('2d'), grd = g.createLinearGradient(0, this.three.skyCanvas.height, 0, 0);
      grd.addColorStop(0, rgb(sky.hor)); grd.addColorStop(1, rgb(sky.top));
      g.fillStyle = grd; g.fillRect(0, 0, 8, 256); this.three.skyTex.needsUpdate = true;
      this.three.fog = this.three.scene.fog; this.three.scene.fog.color = new T.Color(rgb(sky.hor));
      this.three.sun.intensity = 0.15 + 1.05 * Math.max(0, sky.light);
      this.three.hemi.intensity = 0.25 + 0.5 * sky.light;
      const su = enu(this.observer, this.observer.lat + Math.cos(sun.az*D2R)*0.1, this.observer.lon + Math.sin(sun.az*D2R)*0.1, 0);
      const sd = new T.Vector3(Math.sin(sun.az*D2R)*Math.cos(sun.el*D2R), Math.sin(sun.el*D2R), -Math.cos(sun.az*D2R)*Math.cos(sun.el*D2R));
      this.three.sun.position.copy(sd.clone().multiplyScalar(200000));
      this.three.sunBall.position.copy(sd.clone().multiplyScalar(360000));
      this.three.sunBall.visible = sun.el > -2;
      // aircraft
      const positions = []; let readout = '';
      for (const tr of this.tracks) {
        const m = this.three.meshes[tr.id]; if (!m) continue;
        const r = this._sampleRaw(tr, t);
        const vis = !!r; m.plane.visible = vis; if (m.label) m.label.visible = vis;
        if (!vis) continue;
        const e = enu(this.observer, r.lat, r.lon, r.alt);
        m.plane.position.set(e.x, e.y, e.z);
        const e2 = r.prev ? enu(this.observer, r.prev[1], r.prev[2], r.prev[3]) : null;
        if (e2 && (e.x!==e2.x || e.z!==e2.z || e.y!==e2.y)) m.plane.lookAt(e.x + (e.x-e2.x), e.y + (e.y-e2.y), e.z + (e.z-e2.z));
        if (m.label) { m.label.position.set(e.x, e.y + 12*22*0.7, e.z); const s = Math.max(300, e.d*0.03); m.label.scale.set(s*m.label.userData.aspect, s, 1); }
        positions.push({ id: tr.id, color: tr.color, label: tr.label, lat: r.lat, lon: r.lon, alt: r.alt });
        if (!readout) { const el = Math.atan2(e.up, Math.max(e.d,1))*R2D, dist = Math.hypot(e.d, e.up);
          readout = `bearing <b>${compass(e.az)}</b> · elevation <b>${el.toFixed(0)}°</b> · range <b>${dist>=1000?(dist/1000).toFixed(1)+' km':Math.round(dist)+' m'}</b> · alt <b>${r.alt!=null?Math.round(r.alt).toLocaleString()+' ft':'—'}</b>`; }
      }
      const ro = $('gv-readout');
      if (ro) { const sunTxt = sun.el > 0 ? `☀ ${sun.el.toFixed(0)}°` : sun.el > -6 ? '🌆 dusk' : '🌙 night';
        ro.innerHTML = (readout || 'no aircraft up at this moment') + ` &nbsp;·&nbsp; ${sunTxt}` + (this.live ? '' : ` &nbsp;·&nbsp; ${new Date(t).toLocaleTimeString()}`); }
      this.onTime(t, positions);
    }
    _loadGround() {
      if (!this.three || !this.observer) return;
      let maxKm = 3; for (const tr of this.tracks) for (const p of tr.points) { if (p[1]==null) continue; maxKm = Math.max(maxKm, haversineM(this.observer.lat,this.observer.lon,p[1],p[2])/1000); }
      const halfKm = Math.min(40, Math.max(4, maxKm * 1.25)), sizeM = halfKm * 2000;
      this.three.ground.geometry.dispose(); this.three.ground.geometry = new this.three.T.PlaneGeometry(sizeM, sizeM);
      this.three.scene.fog.far = sizeM * 0.8;
      const dLat = halfKm/111, dLon = halfKm/(111*Math.cos(this.observer.lat*D2R));
      const bbox = [this.observer.lon-dLon, this.observer.lat-dLat, this.observer.lon+dLon, this.observer.lat+dLat].join(',');
      const url = `${ESRI}?bbox=${bbox}&bboxSR=4326&imageSR=4326&size=1024,1024&format=jpg&transparent=false&f=image`;
      const loader = new this.three.T.TextureLoader(); loader.setCrossOrigin('anonymous');
      loader.load(url, tex => { if (!this.three) return; this.three.groundMat.map = tex; this.three.groundMat.color.set(0xffffff); this.three.groundMat.needsUpdate = true; }, undefined, () => {});
    }
    _resize() {
      if (!this.three || !this._open) return;
      const c = $('gv-canvas'), w = c.clientWidth || c.getBoundingClientRect().width, h = c.clientHeight || 300;
      this.three.renderer.setSize(w, h, false); this.three.camera.aspect = w / Math.max(1, h); this.three.camera.updateProjectionMatrix();
    }
    _start() { if (this._raf) return; const loop = () => { if (!this._open) return; this._raf = requestAnimationFrame(loop);
      if (this.mode === 'fly') this.three.controls.update();   // FPV ("stand") sets the camera itself
      this.three.renderer.render(this.three.scene, this.three.camera); }; this._raf = requestAnimationFrame(loop); }
    _stop() { if (this._raf) { cancelAnimationFrame(this._raf); this._raf = 0; } }
    play() {
      if (this.live) return; this.playing = true; const pl = $('gv-play'); if (pl) pl.textContent = '❚❚';
      const span = this.tmax - this.tmin || 1; if (this.t >= this.tmax) this.t = this.tmin;
      this.timer = setInterval(() => { this.t += span/240; if (this.t >= this.tmax) { this.t = this.tmax; this.pause(); } this.setTime(this.t); }, 55);
    }
    pause() { this.playing = false; if (this.timer) { clearInterval(this.timer); this.timer = null; } const pl = $('gv-play'); if (pl) pl.textContent = '▶'; }
    _fallback() {
      const c = $('gv-canvas'); if (!c || !c.getContext) return;
      const ctx = c.getContext('2d'); if (!ctx) return;
      const w = c.clientWidth || 600, h = c.clientHeight || 300; c.width = w; c.height = h;
      ctx.fillStyle = '#0a0e16'; ctx.fillRect(0,0,w,h); ctx.fillStyle = '#9aa3b2'; ctx.font = '13px system-ui';
      ctx.fillText('3D view needs WebGL / Three.js — could not load it here.', 16, h/2);
    }
  }
  global.GroundView = GroundView;
  global.GroundViewUtil = { sunPosition, compass };
})(window);
