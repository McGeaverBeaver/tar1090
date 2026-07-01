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

  // camera presets — icon/label for the header button, plus the field of view each one uses
  const VIEW_META = {
    chase:   { icon: '🎥', name: 'Chase',   fov: 55 },
    cockpit: { icon: '🛫', name: 'Cockpit', fov: 74 },
    wing:    { icon: '🛩️', name: 'Wing',    fov: 66 },
    stand:   { icon: '🧍', name: 'Stand',   fov: 58 },
    orbit:   { icon: '🛰️', name: 'Orbit',   fov: 58 },
  };

  // rough aircraft-kind classifier (by ICAO type / ADS-B category / military flag) so we can draw a
  // fitting silhouette. Deliberately small + editable; unknown -> airliner.
  const S = str => new Set(str.trim().split(/\s+/));
  const KIND_HELI = S('EC20 EC25 EC30 EC35 EC45 EC55 EC30 H125 H135 H145 H500 H60 AS50 AS55 AS65 AS32 A109 A119 A139 A169 B06 B407 B412 B429 B430 B505 R22 R44 R66 S76 S92 S61 S64 MI8 MI17 MI24 UH1 EH10 NH90 CH47 H47 B47G B47 GAZL EXPL LYNX PUMA SUPUMA');
  const KIND_JET  = S('F16 F18 F15 F22 F35 F14 F4 F5 A10 AV8B HARR EUFI TYPH RAFL RFAL GRIP JAS39 J39 MG29 MG31 MG25 SU25 SU27 SU30 SU57 M2000 MIR2 F117 T38 T50 L39 L59 HAWK GNAT VAMP HUNT JPRO A4 MB33');
  const KIND_GA   = S('C150 C152 C162 C170 C172 C72R C175 C177 C180 C182 C82R C185 C188 C206 C207 C210 P28A P28B P28R P28S P28T PA28 PA38 PA18 PA22 PA24 PA25 PA32 P32R PA46 SR20 SR22 S22T DA40 DA42 DA20 DV20 M20P M20T M20J AA1 AA5 BE33 BE35 BE36 BE23 BE24 BE19 RV3 RV4 RV6 RV7 RV8 RV9 RV10 RV14 GLAS GLST TOBA TB10 TB20 TB21 DR40 CH7 CH70 KITF PC7 PC9 PC12 PC21 TBM7 TBM8 TBM9 T6 AT6 SNJ HRVD P51 SPIT HURI CORS T28 YK52 YK55 YK18 C208 DHC2 DHC6 BE58 BE76');

  class GroundView {
    constructor(o) {
      o = o || {};
      this.altColor = o.altColor || (() => '#9bd');
      this.onTime = o.onTime || (() => {});
      this.onClose = o.onClose || (() => {});
      this.observer = null; this.tracks = []; this.t = 0; this.tmin = 0; this.tmax = 0;
      this.live = false; this._open = false; this.three = null;
      // camera "views": chase / cockpit / wing lock onto the aircraft and follow it like a game cam;
      // stand = first-person from the ground, orbit = free fly-around. Tap the header button to cycle.
      this.views = ['chase', 'cockpit', 'wing', 'stand', 'orbit'];
      this.view = 'chase';
      this.fpv = { yaw: 0, pitch: 18, fov: 58 };
      this._span = 264; this._follow = null; this._snapCam = true; this._followIdx = 0;
      this.playing = false; this.speed = 1; this._speeds = [0.5, 1, 2, 4]; this._baseRate = 1; this._groundToken = 0;
      this._wire();
    }
    _wire() {
      const sc = $('gv-scrub'); if (sc) sc.oninput = () => { this.pause(); this.setTime(+sc.value); };
      const pl = $('gv-play'); if (pl) pl.onclick = () => this.playing ? this.pause() : this.play();
      const cl = $('gv-close'); if (cl) cl.onclick = () => this.close();
      const md = $('gv-mode'); if (md) md.onclick = () => this.cycleView();
      const sp = $('gv-speed'); if (sp) sp.onclick = () => this.cycleSpeed();
      const fs = $('gv-fs'); if (fs) fs.onclick = () => {
        const el = $('groundview');
        if (document.fullscreenElement) document.exitFullscreen();
        else if (el.requestFullscreen) el.requestFullscreen();
      };
      document.addEventListener('fullscreenchange', () => setTimeout(() => this._resize(), 60));
      window.addEventListener('resize', () => this._resize());
    }

    // ---- public API -------------------------------------------------------
    open(tracks, observer, opts) {
      opts = opts || {}; this.live = !!opts.live;
      if (observer) this.observer = { lat: observer.lat, lon: observer.lon };
      this._open = true; $('groundview').classList.add('on');
      const sc = $('gv-scrub'), pl = $('gv-play'), sp = $('gv-speed');
      if (sc) sc.style.display = this.live ? 'none' : '';
      if (pl) pl.style.display = this.live ? 'none' : '';
      if (sp) { sp.style.display = this.live ? 'none' : ''; sp.textContent = this.speed + '×'; }
      if (!this._ensure()) { this._fallback(); return; }
      this.setTracks(tracks); this._loadGround(); this.projectAll(); this.setView(this.view); this.setTime(this.tmax);
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
      const spanSec = (isFinite(lo) && isFinite(hi)) ? (hi - lo) / 1000 : 0;
      this._baseRate = Math.max(0.2, spanSec / 45);          // play the whole track in ~45 s at 1× (smooth + watchable)
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
      // modern colour pipeline: sRGB output + filmic tone mapping so lighting reads rich, not flat
      if (T.sRGBEncoding != null) renderer.outputEncoding = T.sRGBEncoding;
      if (T.ACESFilmicToneMapping != null) { renderer.toneMapping = T.ACESFilmicToneMapping; renderer.toneMappingExposure = 1.15; }
      const scene = new T.Scene();
      const camera = new T.PerspectiveCamera(58, 1, 1, 600000);
      camera.position.set(0, 2, 0.1);
      const controls = new T.OrbitControls(camera, renderer.domElement);
      controls.enableDamping = true; controls.dampingFactor = 0.08;
      controls.minDistance = 4; controls.maxDistance = 300000; controls.zoomSpeed = 1.2;
      // first-person ("stand") controls: look around in place by dragging, zoom = field of view
      const dom = renderer.domElement, ptrs = new Map(); let pinch = 0;
      const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
      dom.addEventListener('pointerdown', e => { if (this.view !== 'stand') return; ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY }); dom.setPointerCapture(e.pointerId); });
      dom.addEventListener('pointermove', e => {
        if (this.view !== 'stand' || !ptrs.has(e.pointerId)) return;
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
      dom.addEventListener('wheel', e => { if (this.view !== 'stand') return; e.preventDefault();
        this.fpv.fov = clamp(this.fpv.fov + Math.sign(e.deltaY) * 3, 20, 85); camera.fov = this.fpv.fov; camera.updateProjectionMatrix(); }, { passive: false });
      // sky dome
      const skyCanvas = document.createElement('canvas'); skyCanvas.width = 8; skyCanvas.height = 256;
      const skyTex = new T.CanvasTexture(skyCanvas); if (T.sRGBEncoding != null) skyTex.encoding = T.sRGBEncoding;
      const sky = new T.Mesh(new T.SphereGeometry(400000, 32, 16),
        new T.MeshBasicMaterial({ map: skyTex, side: T.BackSide, depthWrite: false, fog: false, toneMapped: false }));
      scene.add(sky);
      // lights
      const hemi = new T.HemisphereLight(0xbfd4ff, 0x202830, 0.7); scene.add(hemi);
      const sun = new T.DirectionalLight(0xffffff, 1.0); scene.add(sun);
      const sunBall = new T.Mesh(new T.SphereGeometry(4000, 24, 16),
        new T.MeshBasicMaterial({ color: 0xfff3c0, fog: false, toneMapped: false })); scene.add(sunBall);
      // soft glow around the sun (golden hour) + a night starfield
      const glow = new T.Sprite(new T.SpriteMaterial({ map: this._radialTex(T, '255,236,186'), transparent: true, opacity: 0, depthWrite: false, depthTest: false, blending: T.AdditiveBlending, fog: false, toneMapped: false }));
      glow.scale.set(150000, 150000, 1); scene.add(glow);
      const sv = []; for (let i = 0; i < 1500; i++) { const u = Math.random()*2-1, th = Math.random()*Math.PI*2, ss = Math.sqrt(1-u*u);
        sv.push(388000*ss*Math.cos(th), Math.abs(388000*u)*0.92 + 1500, 388000*ss*Math.sin(th)); }
      const starGeo = new T.BufferGeometry(); starGeo.setAttribute('position', new T.Float32BufferAttribute(sv, 3));
      const stars = new T.Points(starGeo, new T.PointsMaterial({ color: 0xfdfdff, size: 2, sizeAttenuation: false, transparent: true, opacity: 0, depthWrite: false, fog: false, toneMapped: false })); scene.add(stars);
      // ground — edges fade into the haze (radial alpha) so it reads as atmosphere, not a floating square
      const fadeC = document.createElement('canvas'); fadeC.width = fadeC.height = 256;
      const fg = fadeC.getContext('2d'), fgrd = fg.createRadialGradient(128, 128, 0, 128, 128, 128);
      fgrd.addColorStop(0, '#fff'); fgrd.addColorStop(0.66, '#fff'); fgrd.addColorStop(1, '#000');
      fg.fillStyle = fgrd; fg.fillRect(0, 0, 256, 256);
      const groundFade = new T.CanvasTexture(fadeC); groundFade.userData.shared = true;
      const groundMat = new T.MeshStandardMaterial({ color: 0x2a3340, roughness: 1, metalness: 0,
        transparent: true, alphaMap: groundFade });
      const ground = new T.Mesh(new T.PlaneGeometry(1, 1), groundMat);
      ground.rotation.x = -Math.PI / 2; ground.renderOrder = -1; scene.add(ground);
      // drifting billboard clouds (populated to fit the area in _loadGround)
      const cloudMat = new T.SpriteMaterial({ map: this._cloudTex(T), transparent: true, opacity: 0.4, depthWrite: false });
      const clouds = new T.Group(); scene.add(clouds);
      const fleet = new T.Group(); scene.add(fleet);
      scene.fog = new T.Fog(0x9fb4d8, 2000, 60000);
      // shared assets for the per-plane ground shadows
      this._shadowTex = this._radialTex(T, '0,0,0'); this._shadowTex.userData.shared = true;
      this._shadowGeo = new T.PlaneGeometry(1, 1); this._shadowGeo.userData.shared = true;
      // image-based lighting: reflect the sky off the aircraft + ground for a modern PBR look (refreshed in setTime)
      let pmrem = null; try { pmrem = new T.PMREMGenerator(renderer); pmrem.compileEquirectangularShader(); } catch (e) { pmrem = null; }
      this.three = { T, renderer, scene, camera, controls, sky, skyCanvas, skyTex, hemi, sun, sunBall, glow, stars, ground, groundMat, clouds, cloudMat, fleet, pmrem, envRT: null, raf: 0, meshes: {} };
      return true;
    }
    _radialTex(T, rgbStr) {
      const c = document.createElement('canvas'); c.width = c.height = 128;
      const g = c.getContext('2d'), grd = g.createRadialGradient(64, 64, 0, 64, 64, 64);
      grd.addColorStop(0, `rgba(${rgbStr},1)`); grd.addColorStop(0.35, `rgba(${rgbStr},0.55)`); grd.addColorStop(1, `rgba(${rgbStr},0)`);
      g.fillStyle = grd; g.fillRect(0, 0, 128, 128);
      return new T.CanvasTexture(c);
    }
    _cloudTex(T) {                                           // puffy cumulus billboard (a few soft blobs)
      const c = document.createElement('canvas'); c.width = 256; c.height = 128;
      const g = c.getContext('2d');
      for (let i = 0; i < 9; i++) {
        const x = 44 + Math.random() * 168, y = 48 + Math.random() * 36, r = 22 + Math.random() * 28;
        const gr = g.createRadialGradient(x, y, 0, x, y, r);
        gr.addColorStop(0, 'rgba(255,255,255,0.8)'); gr.addColorStop(1, 'rgba(255,255,255,0)');
        g.fillStyle = gr; g.fillRect(0, 0, 256, 128);
      }
      const t = new T.CanvasTexture(c); t.userData.shared = true; return t;
    }
    _makeClouds(halfM) {                                     // scatter a drifting cloud layer over the area
      const th = this.three; if (!th) return;
      while (th.clouds.children.length) th.clouds.remove(th.clouds.children[0]);   // sprites share cloudMat
      for (let i = 0; i < 12; i++) {
        const sp = new th.T.Sprite(th.cloudMat);
        const a = Math.random() * Math.PI * 2, rr = Math.sqrt(Math.random()) * halfM * 0.85;
        sp.position.set(Math.cos(a) * rr, 1300 + Math.random() * 1500, Math.sin(a) * rr);
        const sx = 1800 + Math.random() * 2800; sp.scale.set(sx, sx * 0.42, 1);
        sp.userData.v = 4 + Math.random() * 7;               // gentle eastward drift, m/s
        th.clouds.add(sp);
      }
      th.clouds.userData.half = halfM;
    }
    _kind(tr) {
      const cat = (tr.category || '').toUpperCase(), t = (tr.type || '').toUpperCase();
      if (cat === 'A7' || KIND_HELI.has(t)) return 'heli';
      if (KIND_JET.has(t)) return 'jet';
      if (KIND_GA.has(t) || (cat === 'A1' && !KIND_JET.has(t))) return 'ga';
      return 'airliner';
    }
    _planeMesh(color, spanM, kind) {
      // Object3D.lookAt() aims the local +Z at the target, so the nose points +Z (tail at -Z).
      const T = this.three.T, g = new T.Group(), s = spanM, spin = [];
      const mat = new T.MeshStandardMaterial({ color: new T.Color(color), roughness: 0.4, metalness: 0.3, envMapIntensity: 1.1 });
      const dark = new T.MeshStandardMaterial({ color: 0x2f333b, roughness: 0.5, metalness: 0.55, envMapIntensity: 1.1 });
      const glass = new T.MeshStandardMaterial({ color: 0x0b0f18, roughness: 0.12, metalness: 0.2, envMapIntensity: 1.5 });
      const discMat = () => new T.MeshBasicMaterial({ color: 0x9aa3b2, transparent: true, opacity: 0.14, side: T.DoubleSide, depthWrite: false, toneMapped: false });
      // swept trapezoid wing (right side + mirror) — reused for wings, tailplanes, fins
      const wing = (half, cR, cT, sweep, th, zoff, yoff) => {
        const sh = new T.Shape();
        sh.moveTo(0, cR*0.5); sh.lineTo(half, -sweep + cT*0.5); sh.lineTo(half, -sweep - cT*0.5); sh.lineTo(0, -cR*0.5); sh.closePath();
        const geo = new T.ExtrudeGeometry(sh, { depth: th, bevelEnabled: false });
        geo.translate(0, 0, -th/2); geo.rotateX(Math.PI/2);
        const r = new T.Mesh(geo, mat), l = new T.Mesh(geo, mat); l.scale.x = -1;
        r.position.set(0, yoff||0, zoff); l.position.set(0, yoff||0, zoff); return [r, l];
      };
      const finMesh = (h, z) => {
        const fs = new T.Shape(); fs.moveTo(s*0.08, 0); fs.lineTo(-s*0.08, 0); fs.lineTo(-s*0.08 - s*0.05, h); fs.lineTo(-s*0.05, h); fs.closePath();
        const fg = new T.ExtrudeGeometry(fs, { depth: s*0.014, bevelEnabled: false }); fg.translate(0, 0, -s*0.007); fg.rotateY(-Math.PI/2);
        const m = new T.Mesh(fg, mat); m.position.z = z; return m;
      };
      const blades = (grp, len, wide, n, ax) => { for (let b = 0; b < n; b++) { const bl = new T.Mesh(new T.BoxGeometry(len, s*0.008, wide), dark); bl.rotation[ax] = b * Math.PI / n; grp.add(bl); } };

      if (kind === 'heli') {
        const pod = new T.Mesh(new T.SphereGeometry(s*0.12, 16, 12), mat); pod.scale.set(0.8, 0.92, 1.5); g.add(pod);
        const boom = new T.Mesh(new T.CylinderGeometry(s*0.022, s*0.012, s*0.5, 10), mat); boom.rotation.x = Math.PI/2; boom.position.z = -s*0.36; g.add(boom);
        g.add(finMesh(s*0.11, -s*0.58));
        const rotor = new T.Group(); rotor.position.y = s*0.15;
        const rd = new T.Mesh(new T.CircleGeometry(s*0.62, 30), discMat()); rd.rotation.x = -Math.PI/2; rd.material.opacity = 0.1; rotor.add(rd);
        blades(rotor, s*1.24, s*0.03, 2, 'y'); rotor.add(new T.Mesh(new T.CylinderGeometry(s*0.012, s*0.012, s*0.07, 8), dark));
        g.add(rotor); spin.push({ obj: rotor, axis: 'y', rate: 26 });
        const tail = new T.Group(); tail.position.set(s*0.02, s*0.03, -s*0.585);
        const td = new T.Mesh(new T.CircleGeometry(s*0.14, 16), discMat()); td.rotation.y = Math.PI/2; tail.add(td);
        blades(tail, s*0.006, s*0.28, 2, 'x'); g.add(tail); spin.push({ obj: tail, axis: 'x', rate: 60 });
        for (const sx of [1, -1]) { const sk = new T.Mesh(new T.CylinderGeometry(s*0.008, s*0.008, s*0.34, 8), dark); sk.rotation.x = Math.PI/2; sk.position.set(sx*s*0.09, -s*0.13, s*0.02); g.add(sk); }
      } else if (kind === 'ga') {
        const fuse = new T.Mesh(new T.CapsuleGeometry(s*0.05, s*0.42, 6, 16), mat); fuse.rotation.x = Math.PI/2; g.add(fuse);
        const nose = new T.Mesh(new T.ConeGeometry(s*0.05, s*0.10, 16), mat); nose.rotation.x = Math.PI/2; nose.position.z = s*0.28; g.add(nose);
        g.add(...wing(s*0.56, s*0.14, s*0.11, s*0.015, s*0.02, s*0.03, s*0.07));   // high straight wing
        g.add(...wing(s*0.18, s*0.09, s*0.06, s*0.03, s*0.014, -s*0.32));
        g.add(finMesh(s*0.15, -s*0.28));
        const prop = new T.Group(); prop.position.z = s*0.34;
        prop.add(new T.Mesh(new T.SphereGeometry(s*0.022, 8, 6), dark));
        const pd = new T.Mesh(new T.CircleGeometry(s*0.17, 20), discMat()); pd.material.opacity = 0.16; prop.add(pd);
        blades(prop, s*0.32, s*0.006, 2, 'z'); g.add(prop); spin.push({ obj: prop, axis: 'z', rate: 45 });
        const canopy = new T.Mesh(new T.SphereGeometry(s*0.05, 12, 10), glass); canopy.scale.set(1, 0.72, 1.5); canopy.position.set(0, s*0.045, s*0.05); g.add(canopy);
      } else if (kind === 'jet') {
        const fuse = new T.Mesh(new T.CapsuleGeometry(s*0.026, s*0.72, 6, 16), mat); fuse.rotation.x = Math.PI/2; g.add(fuse);
        const nose = new T.Mesh(new T.ConeGeometry(s*0.026, s*0.26, 16), mat); nose.rotation.x = Math.PI/2; nose.position.z = s*0.49; g.add(nose);
        g.add(...wing(s*0.34, s*0.30, s*0.02, s*0.27, s*0.016, -s*0.06));           // delta wing
        g.add(...wing(s*0.15, s*0.10, s*0.03, s*0.08, s*0.012, -s*0.44));
        g.add(finMesh(s*0.17, -s*0.42));
        const canopy = new T.Mesh(new T.SphereGeometry(s*0.026, 12, 10), glass); canopy.scale.set(1, 0.85, 2.6); canopy.position.set(0, s*0.02, s*0.30); g.add(canopy);
        const ex = new T.Mesh(new T.CylinderGeometry(s*0.022, s*0.03, s*0.07, 14), dark); ex.rotation.x = Math.PI/2; ex.position.z = -s*0.52; g.add(ex);
      } else {                                                                        // airliner (default)
        const fuse = new T.Mesh(new T.CapsuleGeometry(s*0.030, s*0.60, 6, 18), mat); fuse.rotation.x = Math.PI/2; g.add(fuse);
        const nose = new T.Mesh(new T.ConeGeometry(s*0.030, s*0.16, 18), mat); nose.rotation.x = Math.PI/2; nose.position.z = s*0.38; g.add(nose);
        g.add(...wing(s*0.5,  s*0.17, s*0.05,  s*0.12, s*0.018, -s*0.02));
        g.add(...wing(s*0.19, s*0.09, s*0.035, s*0.05, s*0.014, -s*0.40));
        g.add(finMesh(s*0.20, -s*0.36));
        for (const sx of [1, -1]) { const n = new T.Mesh(new T.CylinderGeometry(s*0.036, s*0.03, s*0.17, 14), dark); n.rotation.x = Math.PI/2; n.position.set(sx*s*0.2, -s*0.05, s*0.02); g.add(n); }
        const canopy = new T.Mesh(new T.SphereGeometry(s*0.03, 12, 10), glass); canopy.scale.set(1, 0.62, 2.1); canopy.position.set(0, s*0.028, s*0.22); g.add(canopy);
      }
      // red anti-collision strobe on top — double-flash pulsed in the render loop, like the real thing
      const strobeMat = new T.MeshBasicMaterial({ color: 0xff4444, transparent: true, opacity: 0.25, toneMapped: false });
      const strobe = new T.Mesh(new T.SphereGeometry(s*0.014, 8, 6), strobeMat);
      strobe.position.set(0, { heli: s*0.18, ga: s*0.17, jet: s*0.19, airliner: s*0.22 }[kind] || s*0.2,
                             { heli: 0,      ga: -s*0.3, jet: -s*0.43, airliner: -s*0.4 }[kind] || -s*0.4);
      g.add(strobe);
      g.userData.strobe = strobeMat;
      g.userData.spin = spin;
      return g;
    }
    projectAll() {
      if (!this.three || !this.observer) return;
      const T = this.three.T, fleet = this.three.fleet;
      // dispose the previous fleet's GPU resources (skip shared shadow geo/texture)
      fleet.traverse(o => {
        if (o.geometry && !(o.geometry.userData && o.geometry.userData.shared)) o.geometry.dispose();
        if (o.material) { const mm = o.material; if (mm.map && !(mm.map.userData && mm.map.userData.shared)) mm.map.dispose(); mm.dispose(); }
      });
      while (fleet.children.length) fleet.remove(fleet.children[0]);
      this.three.meshes = {};
      const EXAG = 14, span = 12 * EXAG; this._span = span;  // exaggerate ~12 m wingspan so the model is visible
      for (const tr of this.tracks) {
        const col = tr.color || '#6cc1ff';
        // trail: a flat altitude-coloured ribbon (smoke-trail look), not a 1-px GL line
        const stepN = Math.max(1, Math.floor(tr.points.length / 400)), pts3 = [], cols3 = [];
        for (let i = 0; i < tr.points.length; i += stepN) { const p = tr.points[i]; if (p[1] == null) continue;
          pts3.push(enu(this.observer, p[1], p[2], p[3]));
          cols3.push(new T.Color(this.altColor(p[3]))); }
        const ribbon = this._ribbon(T, pts3, cols3, span * 0.16);
        if (ribbon) fleet.add(ribbon);
        const plane = this._planeMesh(col, span, this._kind(tr)); fleet.add(plane);
        const shadow = new T.Mesh(this._shadowGeo, new T.MeshBasicMaterial({ map: this._shadowTex, color: 0x000000, transparent: true, opacity: 0.34, depthWrite: false, fog: false, toneMapped: false }));
        shadow.rotation.x = -Math.PI / 2; shadow.position.y = 1.5; fleet.add(shadow);
        this.three.meshes[tr.id] = { plane, shadow, track: tr };
      }
    }
    _ribbon(T, pts, cols, width) {                           // horizontal triangle-strip ribbon through the fixes
      const n = pts.length; if (n < 2) return null;
      const pos = new Float32Array(n * 6), col = new Float32Array(n * 6), idx = [];
      for (let i = 0; i < n; i++) {
        const p = pts[i], q = pts[Math.min(n - 1, i + 1)], o = pts[Math.max(0, i - 1)];
        let dx = q.x - o.x, dz = q.z - o.z; const L = Math.hypot(dx, dz) || 1; dx /= L; dz /= L;
        const px = -dz * width / 2, pz = dx * width / 2;     // horizontal perpendicular to travel
        pos.set([p.x - px, p.y, p.z - pz, p.x + px, p.y, p.z + pz], i * 6);
        const c = cols[i]; col.set([c.r, c.g, c.b, c.r, c.g, c.b], i * 6);
        if (i < n - 1) idx.push(2*i, 2*i+1, 2*i+2, 2*i+1, 2*i+3, 2*i+2);
      }
      const g = new T.BufferGeometry();
      g.setAttribute('position', new T.BufferAttribute(pos, 3));
      g.setAttribute('color', new T.BufferAttribute(col, 3));
      g.setIndex(idx);
      return new T.Mesh(g, new T.MeshBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.45,
        side: T.DoubleSide, depthWrite: false, toneMapped: false }));
    }
    _centroid() {
      let n = 0, cx = 0, cy = 0, cz = 0;
      for (const tr of this.tracks) for (const p of tr.points) { if (p[1] == null) continue; const e = enu(this.observer, p[1], p[2], p[3]); cx += e.x; cy += e.y; cz += e.z; n++; }
      return n ? { x: cx/n, y: cy/n, z: cz/n, n } : { x: 0, y: 500, z: -2000, n: 0 };
    }
    fitView() {
      if (!this.three) return;
      if (this.view === 'stand') { this._standAim(); return; }
      if (this._isFollow()) { this._snapCam = true; return; }   // chase/cockpit/wing snap onto the plane next frame
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
    setView(v) {
      if (this.views.indexOf(v) < 0) v = 'chase';
      this.view = v;
      const meta = VIEW_META[v], md = $('gv-mode'); if (md) md.textContent = meta.icon + ' ' + meta.name;
      if (!this.three) return;
      const cam = this.three.camera, c = this.three.controls;
      if (v === 'orbit') { c.enabled = true; cam.fov = meta.fov; cam.updateProjectionMatrix(); this.fitView(); }
      else if (v === 'stand') { c.enabled = false; this.fpv.fov = meta.fov; cam.fov = meta.fov; cam.updateProjectionMatrix(); this._standAim(); }
      else { c.enabled = false; this._snapCam = true; }   // chase / cockpit / wing: the render loop drives the camera
    }
    cycleView() { this.setView(this.views[(this.views.indexOf(this.view) + 1) % this.views.length]); }
    _isFollow() { return this.view === 'chase' || this.view === 'cockpit' || this.view === 'wing'; }
    _followMesh() {                                          // the aircraft the game-cam locks onto (first one currently up)
      const n = this.tracks.length; if (!n || !this.three) return null;
      for (let k = 0; k < n; k++) { const tr = this.tracks[(this._followIdx + k) % n];
        const m = this.three.meshes[tr.id]; if (m && m.plane.visible) return m; }
      return null;
    }
    _followCam() {                                           // place + smoothly chase the camera relative to the plane
      const T = this.three.T, cam = this.three.camera, m = this._followMesh();
      if (!m) return;                                        // nobody up at this moment -> hold the last camera
      const pl = m.plane, p = pl.position, s = this._span || 264;
      const fwd = new T.Vector3(0, 0, 1).applyQuaternion(pl.quaternion);   // nose direction (the model's local +Z)
      if (fwd.lengthSq() < 1e-6) fwd.set(0, 0, -1); else fwd.normalize();
      const up = new T.Vector3(0, 1, 0), right = new T.Vector3().crossVectors(fwd, up);
      if (right.lengthSq() < 1e-6) right.set(1, 0, 0); else right.normalize();
      const dPos = new T.Vector3(), dTgt = new T.Vector3(); let fov, k;
      if (this.view === 'cockpit') {                         // sit just behind the nose, look down the heading
        dPos.copy(p).addScaledVector(fwd, s * 0.32).addScaledVector(up, s * 0.11);
        dTgt.copy(p).addScaledVector(fwd, s * 60); fov = VIEW_META.cockpit.fov; k = 0.24;
      } else if (this.view === 'wing') {                     // ride the wingtip, fuselage to one side
        dPos.copy(p).addScaledVector(right, s * 0.55).addScaledVector(up, s * 0.10).addScaledVector(fwd, -s * 0.05);
        dTgt.copy(p).addScaledVector(fwd, s * 3.5).addScaledVector(right, -s * 0.25); fov = VIEW_META.wing.fov; k = 0.18;
      } else {                                               // chase: behind + above, trailing the plane down its own path
        dPos.copy(p).addScaledVector(fwd, -s * 3.2).addScaledVector(up, s * 1.15);
        dTgt.copy(p).addScaledVector(fwd, s * 1.6); fov = VIEW_META.chase.fov; k = 0.09;
      }
      const fc = this._follow || (this._follow = { pos: dPos.clone(), tgt: dTgt.clone(), fov });
      if (this._snapCam) { fc.pos.copy(dPos); fc.tgt.copy(dTgt); fc.fov = fov; this._snapCam = false; }
      else { fc.pos.lerp(dPos, k); fc.tgt.lerp(dTgt, k); fc.fov += (fov - fc.fov) * 0.15; }
      cam.up.set(0, 1, 0); cam.position.copy(fc.pos); cam.lookAt(fc.tgt);
      if (Math.abs(cam.fov - fc.fov) > 0.02) { cam.fov = fc.fov; cam.updateProjectionMatrix(); }
    }
    // Catmull-Rom through the surrounding fixes -> a smooth, natural curve (not straight segments
    // with a snap at every data point). Position + altitude are splined; heading & bank come from it.
    _smooth(track, t) {
      const p = track.points, n = p.length; if (!n) return null;
      if (t < p[0][0] - 4000 || t > p[n-1][0] + 4000) return null;
      let i = 0; while (i < n-1 && p[i+1][0] <= t) i++;
      const a = p[i], b = p[Math.min(n-1, i+1)], p0 = p[Math.max(0, i-1)], p3 = p[Math.min(n-1, i+2)];
      const f = b[0] === a[0] ? 0 : Math.max(0, Math.min(1, (t-a[0])/(b[0]-a[0])));
      const cr = (v0,v1,v2,v3) => { const f2=f*f, f3=f2*f;
        return 0.5*((2*v1) + (-v0+v2)*f + (2*v0-5*v1+4*v2-v3)*f2 + (-v0+3*v1-3*v2+v3)*f3); };
      const alt = (a[3]!=null && b[3]!=null)
        ? (p0[3]!=null && p3[3]!=null ? cr(p0[3],a[3],b[3],p3[3]) : a[3]+(b[3]-a[3])*f)
        : (a[3]!=null ? a[3] : b[3]);
      return { lat: cr(p0[1],a[1],b[1],p3[1]), lon: cr(p0[2],a[2],b[2],p3[2]), alt };
    }
    // full state at time t: smoothed position + fore/aft samples for heading + a coordinated-turn bank
    _state(track, t) {
      const dt = 1200, mid = this._smooth(track, t); if (!mid) return null;
      const a = this._smooth(track, t - dt) || mid, b = this._smooth(track, t + dt) || mid;
      const hA = bearing(a.lat, a.lon, mid.lat, mid.lon), hB = bearing(mid.lat, mid.lon, b.lat, b.lon);
      const dH = ((hB - hA + 540) % 360) - 180;                 // signed heading change over ~dt
      const spd = haversineM(a.lat, a.lon, b.lat, b.lon) / (2 * dt/1000);
      const bank = Math.max(-1.02, Math.min(1.02, Math.atan(spd * (dH*D2R)/(dt/1000) / 9.81)));
      return { lat: mid.lat, lon: mid.lon, alt: mid.alt, a, b, bank, spd };
    }
    setTime(t, fast) {
      this.t = t; const sc = $('gv-scrub'); if (sc && !this.live) sc.value = t;
      if (!this.three || !this.observer) return;
      const T = this.three.T;
      const now = (global.performance && performance.now) ? performance.now() : Date.now();
      // during smooth playback (fast) throttle the slow work — sky repaint, env, readout, map markers — to ~8 Hz;
      // any deliberate setTime (scrub / live tick / fit) refreshes everything.
      const heavy = !fast || (now - (this._hvT || 0)) > 120; if (heavy) this._hvT = now;
      if (heavy) {                                            // time of day: sky, sun, light, reflections
        const sun = sunPosition(t, this.observer.lat, this.observer.lon), sky = skyColours(sun.el);
        const g = this.three.skyCanvas.getContext('2d'), grd = g.createLinearGradient(0, this.three.skyCanvas.height, 0, 0);
        grd.addColorStop(0, rgb(sky.hor)); grd.addColorStop(1, rgb(sky.top));
        g.fillStyle = grd; g.fillRect(0, 0, 8, 256); this.three.skyTex.needsUpdate = true;
        this._updateEnv(sky);
        this.three.scene.fog.color = new T.Color(rgb(sky.hor));
        this.three.sun.intensity = 0.15 + 1.05 * Math.max(0, sky.light);
        this.three.hemi.intensity = 0.25 + 0.5 * sky.light;
        const sd = new T.Vector3(Math.sin(sun.az*D2R)*Math.cos(sun.el*D2R), Math.sin(sun.el*D2R), -Math.cos(sun.az*D2R)*Math.cos(sun.el*D2R));
        this.three.sun.position.copy(sd.clone().multiplyScalar(200000));
        this.three.sunBall.position.copy(sd.clone().multiplyScalar(360000));
        this.three.sunBall.visible = sun.el > -2; this._sun = sun;
        this.three.glow.position.copy(sd.clone().multiplyScalar(330000));
        this.three.glow.material.opacity = Math.max(0, Math.min(0.55, (sun.el + 6) / 24));
        this.three.stars.material.opacity = Math.max(0, Math.min(1, (-4 - sun.el) / 8));
        this._sd = sd; this._light = sky.light;              // for sun-cast shadows + cloud tint below
        this.three.cloudMat.color.setScalar(0.35 + 0.65 * Math.max(0, sky.light));
        this.three.cloudMat.opacity = 0.12 + 0.3 * Math.max(0, sky.light);
      }
      // aircraft transforms — every frame, so motion stays smooth at the display refresh rate
      const positions = []; let readout = '';
      for (const tr of this.tracks) {
        const m = this.three.meshes[tr.id]; if (!m) continue;
        const r = this._state(tr, t);
        const vis = !!r; m.plane.visible = vis; if (m.shadow) m.shadow.visible = vis;
        if (!vis) continue;
        const e = enu(this.observer, r.lat, r.lon, r.alt);
        // low-pass the bank so spline noise doesn't twitch the wings
        m._bank = (m._bank == null) ? r.bank : m._bank + (r.bank - m._bank) * 0.2;
        // keep the (possibly banked) exaggerated model from poking through the ground when it's low
        const clear = this._span * (0.16 + 0.5 * Math.abs(Math.sin(m._bank)));
        const py = Math.max(e.y, clear);
        m.plane.position.set(e.x, py, e.z);
        // smooth 3D heading (climb included) from the spline's fore/aft samples, plus a banking roll
        const ea = enu(this.observer, r.a.lat, r.a.lon, r.a.alt), eb = enu(this.observer, r.b.lat, r.b.lon, r.b.alt);
        const dx = eb.x - ea.x, dy = eb.y - ea.y, dz = eb.z - ea.z;
        if (dx || dy || dz) { m.plane.up.set(0, 1, 0); m.plane.lookAt(e.x + dx, py + dy, e.z + dz); m.plane.rotateZ(-m._bank); }
        if (m.shadow) {                                      // cast along the actual sun direction, faded by daylight
          const sd = this._sd; let sx = e.x, sz2 = e.z;
          if (sd && sd.y > 0.08) { const k2 = Math.min(e.up / sd.y, 12000); sx = e.x - sd.x * k2; sz2 = e.z - sd.z * k2; }
          const sz = (this._span || 264) * (1 + e.up / 1500);
          m.shadow.position.set(sx, 1.5, sz2); m.shadow.scale.set(sz, sz, 1);
          m.shadow.material.opacity = Math.max(0.03, 0.38 - e.up * 0.00012) * Math.max(0.25, this._light == null ? 1 : this._light);
        }
        positions.push({ id: tr.id, color: tr.color, label: tr.label, lat: r.lat, lon: r.lon, alt: r.alt });
        if (!readout) { const el = Math.atan2(e.up, Math.max(e.d,1))*R2D, dist = Math.hypot(e.d, e.up);
          const kt = isFinite(r.spd) ? ` · speed <b>${Math.round(r.spd * 1.94384)} kt</b>` : '';
          readout = `bearing <b>${compass(e.az)}</b> · elevation <b>${el.toFixed(0)}°</b>${kt} · range <b>${dist>=1000?(dist/1000).toFixed(1)+' km':Math.round(dist)+' m'}</b> · alt <b>${r.alt!=null?Math.round(r.alt).toLocaleString()+' ft':'—'}</b>`; }
      }
      if (heavy) {                                            // readout text + map markers (throttled with the slow work)
        const sun = this._sun || sunPosition(t, this.observer.lat, this.observer.lon);
        const ro = $('gv-readout');
        if (ro) { const sunTxt = sun.el > 0 ? `☀ ${sun.el.toFixed(0)}°` : sun.el > -6 ? '🌆 dusk' : '🌙 night';
          ro.innerHTML = (readout || 'no aircraft up at this moment') + ` &nbsp;·&nbsp; ${sunTxt}` + (this.live ? '' : ` &nbsp;·&nbsp; ${new Date(t).toLocaleTimeString()}`); }
        this.onTime(t, positions);
      }
    }
    _updateEnv(sky) {                                        // reflect the current sky off PBR surfaces (throttled by light level)
      const th = this.three; if (!th || !th.pmrem) return;
      const bucket = sky.night ? -1 : Math.round(Math.max(0, sky.light) * 6);
      if (bucket === this._envLevel) return; this._envLevel = bucket;
      try { const rt = th.pmrem.fromEquirectangular(th.skyTex);
        if (th.envRT) th.envRT.dispose(); th.envRT = rt; th.scene.environment = rt.texture; } catch (e) {}
    }
    _loadGround() {
      if (!this.three || !this.observer) return;
      let maxKm = 3; for (const tr of this.tracks) for (const p of tr.points) { if (p[1]==null) continue; maxKm = Math.max(maxKm, haversineM(this.observer.lat,this.observer.lon,p[1],p[2])/1000); }
      const halfKm = Math.min(40, Math.max(4, maxKm * 1.25)), sizeM = halfKm * 2000;
      this.three.ground.geometry.dispose(); this.three.ground.geometry = new this.three.T.PlaneGeometry(sizeM, sizeM);
      this.three.scene.fog.far = sizeM * 0.8;
      this._makeClouds(sizeM / 2);
      const dLat = halfKm/111, dLon = halfKm/(111*Math.cos(this.observer.lat*D2R));
      const bbox = [this.observer.lon-dLon, this.observer.lat-dLat, this.observer.lon+dLon, this.observer.lat+dLat].join(',');
      const mk = px => `${ESRI}?bbox=${bbox}&bboxSR=4326&imageSR=4326&size=${px},${px}&format=jpg&transparent=false&f=image`;
      const loader = new this.three.T.TextureLoader(); loader.setCrossOrigin('anonymous');
      const token = ++this._groundToken;                     // ignore stale loads when the observer is dragged around
      const apply = tex => {
        if (!this.three || token !== this._groundToken) { if (tex && tex.dispose) tex.dispose(); return; }
        const T = this.three.T; if (T.sRGBEncoding != null) tex.encoding = T.sRGBEncoding;
        try { tex.anisotropy = this.three.renderer.capabilities.getMaxAnisotropy(); } catch (e) {}
        const old = this.three.groundMat.map; this.three.groundMat.map = tex; this.three.groundMat.color.set(0xffffff);
        this.three.groundMat.roughness = 0.92; this.three.groundMat.needsUpdate = true;
        if (old && old !== tex) old.dispose();
      };
      // show a quick low-res tile first so the ground appears fast, then sharpen it in the background
      loader.load(mk(1024), tex => { apply(tex); if (this.three && token === this._groundToken) loader.load(mk(2048), apply, undefined, () => {}); }, undefined, () => {});
    }
    _resize() {
      if (!this.three || !this._open) return;
      const c = $('gv-canvas'), w = c.clientWidth || c.getBoundingClientRect().width, h = c.clientHeight || 300;
      this.three.renderer.setSize(w, h, false); this.three.camera.aspect = w / Math.max(1, h); this.three.camera.updateProjectionMatrix();
    }
    _start() {
      if (this._raf) return;
      let last = (global.performance && performance.now) ? performance.now() : Date.now();
      const loop = (ts) => {
        if (!this._open) return; this._raf = requestAnimationFrame(loop);
        const now = ts || ((global.performance && performance.now) ? performance.now() : Date.now());
        const dt = Math.min(0.05, Math.max(0, (now - last) / 1000)); last = now;   // seconds since last frame, clamped over stalls
        if (this.playing && !this.live) {                              // advance the clock by real elapsed time, then interpolate
          this.t += dt * 1000 * this._baseRate * this.speed;
          if (this.t >= this.tmax) { this.t = this.tmax; this.pause(); }
          this.setTime(this.t, true);                                  // 'true' = smooth-playback update (throttles the slow work)
        }
        const ph = (now / 1000) % 1.1;                                 // real strobes double-flash
        const strobeOn = ph < 0.06 || (ph > 0.12 && ph < 0.18);
        for (const id in this.three.meshes) { const m = this.three.meshes[id];   // spin props/rotors, pulse strobes
          if (!m.plane.visible) continue;
          const st = m.plane.userData.strobe; if (st) st.opacity = strobeOn ? 1 : 0.18;
          const sp = m.plane.userData.spin; if (!sp) continue;
          for (const s2 of sp) s2.obj.rotation[s2.axis] += s2.rate * dt; }
        for (const cl of this.three.clouds.children) {                 // clouds drift gently downwind
          cl.position.x += cl.userData.v * dt;
          const half = this.three.clouds.userData.half || 20000;
          if (cl.position.x > half) cl.position.x = -half;
        }
        if (this.view === 'orbit') this.three.controls.update();       // stand sets the camera from pointer drags
        else if (this._isFollow()) this._followCam();                  // chase/cockpit/wing track the aircraft
        this.three.renderer.render(this.three.scene, this.three.camera);
      };
      this._raf = requestAnimationFrame(loop);
    }
    _stop() { if (this._raf) { cancelAnimationFrame(this._raf); this._raf = 0; } }
    play() {
      if (this.live) return; this.playing = true; const pl = $('gv-play'); if (pl) pl.textContent = '❚❚';
      if (this.t >= this.tmax) this.t = this.tmin;                     // restart from the beginning if parked at the end
    }
    pause() { this.playing = false; const pl = $('gv-play'); if (pl) pl.textContent = '▶'; }
    cycleSpeed() {
      const i = (this._speeds.indexOf(this.speed) + 1) % this._speeds.length;
      this.speed = this._speeds[i]; const sp = $('gv-speed'); if (sp) sp.textContent = this.speed + '×';
    }
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
