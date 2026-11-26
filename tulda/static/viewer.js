// tulda interactive viewer. Loads three.js lazily (vendored, see vendor/three-*/VENDOR.md) and
// shows the GLB named in the page data. Rendering is on demand (no animation loop).
const D = JSON.parse(document.getElementById('vdata').textContent);
const $ = id => document.getElementById(id);
const stage = $('stage'), msg = $('msg');
let T = null;                      // THREE namespace once loaded
let scene, camera, renderer, controls, root, grid, bedMesh, bbox, center, radius;
let wire = false, queued = false;
let boxHelper = null, scaleOn = true, labels = [];   // scale reference: bbox edges + mm labels

function say(text, bad) { msg.textContent = text; msg.hidden = !text; msg.classList.toggle('bad', !!bad); }
function hasWebGL() {
  try {
    const c = document.createElement('canvas');
    return !!(window.WebGL2RenderingContext && c.getContext('webgl2')) ||
           !!(window.WebGLRenderingContext && (c.getContext('webgl') || c.getContext('experimental-webgl')));
  } catch (e) { return false; }
}

const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
};

// ---- theme
let dark = store.get('tulda.viewer.dark');
dark = dark === null ? matchMedia('(prefers-color-scheme: dark)').matches : dark === '1';
function applyTheme() {
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  $('theme').textContent = dark ? 'Light background' : 'Dark background';
  if (scene) { scene.background = new T.Color(dark ? 0x17171a : 0xeceef2); recolorGrid(); render(); }
}

function render() {
  if (!renderer || queued) return;
  queued = true;
  requestAnimationFrame(() => { queued = false; renderer.render(scene, camera); placeLabels(); });
}

function size() {
  const w = stage.clientWidth, h = stage.clientHeight;
  if (!w || !h) return;
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  render();
}

// Camera presets in glTF space (Y up). The model is CAD Z-up under a root node that maps
// CAD (x, y, z) to (x, z, -y): top = +Y, bottom = -Y, CAD front (-y) = +Z.
const DIRS = {
  iso: [0.62, 0.55, 0.9], top: [0, 1, 0.0001], bottom: [0, -1, 0.02], front: [0, 0.05, 1],
  back: [0, 0.05, -1], left: [-1, 0.05, 0], right: [1, 0.05, 0],
};
function preset(name) {
  const d = new T.Vector3(...DIRS[name]).normalize();
  const dist = radius / Math.sin(T.MathUtils.degToRad(camera.fov / 2)) * 1.05;
  camera.position.copy(center).addScaledVector(d, dist);
  controls.target.copy(center);
  camera.near = Math.max(radius / 200, 0.01);
  camera.far = radius * 100;
  camera.updateProjectionMatrix();
  controls.update();
  render();
}

function recolorGrid() {
  if (!grid) return;
  const m = Array.isArray(grid.material) ? grid.material : [grid.material];
  m[0].color.set(dark ? 0x4a4a55 : 0xb0b4bc);
  if (m[1]) m[1].color.set(dark ? 0x6a6a78 : 0x8c909a);
}

// Grid squares in a round step (1/5/10/50/100 mm) chosen from the model size: about 5-25
// squares across the model.
const STEPS = [1, 5, 10, 50, 100, 500];
function gridStep(maxDim) { return STEPS.find(s => maxDim / s <= 25) || STEPS[STEPS.length - 1]; }
function buildGrid() {
  const bed = D.info && D.info.bed;           // [x0, y0, x1, y1] in CAD mm
  const s = new T.Vector3(); bbox.getSize(s);
  let step = gridStep(Math.max(s.x, s.y, s.z, 0.001));
  let cx, cz, w, d;
  if (bed) { w = bed[2] - bed[0]; d = bed[3] - bed[1]; cx = (bed[0] + bed[2]) / 2; cz = -(bed[1] + bed[3]) / 2; }
  else {
    w = d = Math.ceil(Math.max(s.x, s.z) * 2 / step) * step || step; cx = center.x; cz = center.z;
  }
  let size = Math.max(w, d);
  while (size / step > 400) step = STEPS.find(x => x > step) || step * 10;   // keep big beds light
  if (bed) size = Math.ceil(size / step) * step;
  const n = Math.max(1, Math.round(size / step));
  grid = new T.GridHelper(size, n, 0x8c909a, 0xb0b4bc);
  const gs = $('gstep'); if (gs) gs.textContent = 'grid: ' + step + ' mm squares';
  grid.position.set(cx, -0.02, cz);
  if (bed) {
    const g = new T.Mesh(new T.PlaneGeometry(w, d), new T.MeshBasicMaterial(
      { color: 0x808090, transparent: true, opacity: 0.12, side: T.DoubleSide, depthWrite: false }));
    g.rotation.x = -Math.PI / 2; g.position.set(cx, -0.05, cz); bedMesh = g; scene.add(g);
  }
  scene.add(grid);
  recolorGrid();
}

// ---- scale reference: the visible parts' bounding box with its edge lengths in mm.
// glTF space is Y-up: CAD X = x, CAD Y = -z, CAD Z = y.
function mm(v) { return v.toFixed(1) + ' mm'; }
function buildScale() {
  if (boxHelper) { scene.remove(boxHelper); boxHelper.geometry.dispose(); }
  labels.forEach(l => l.el.remove()); labels = [];
  const b = visibleBox();
  if (b.isEmpty()) return;
  boxHelper = new T.Box3Helper(b, dark ? 0x4fc58a : 0x2e8b57);
  boxHelper.visible = scaleOn; scene.add(boxHelper);
  const s = b.getSize(new T.Vector3()), c = b.getCenter(new T.Vector3());
  [['X ' + mm(s.x), new T.Vector3(c.x, b.min.y, b.max.z)],
   ['Y ' + mm(s.z), new T.Vector3(b.max.x, b.min.y, c.z)],
   ['Z ' + mm(s.y), new T.Vector3(b.max.x, c.y, b.max.z)]].forEach(([text, at]) => {
    const el = document.createElement('div'); el.className = 'dl'; el.textContent = text; el.hidden = !scaleOn;
    stage.append(el); labels.push({ el, at });
  });
}
function placeLabels() {
  if (!labels.length || !scaleOn) return;
  const w = stage.clientWidth, h = stage.clientHeight, v = new T.Vector3();
  labels.forEach(l => {
    v.copy(l.at).project(camera);
    const off = v.z > 1 || v.z < -1;
    l.el.hidden = off;
    if (!off) { l.el.style.left = ((v.x + 1) / 2 * w) + 'px'; l.el.style.top = ((1 - v.y) / 2 * h) + 'px'; }
  });
}
function setScale(on) {
  scaleOn = on;
  if (boxHelper) boxHelper.visible = on;
  labels.forEach(l => { l.el.hidden = !on; });
  $('scale').setAttribute('aria-pressed', on);
  render();
}

function setWire(on) {
  wire = on;
  root.traverse(o => { if (o.isMesh) o.material.wireframe = on; });
  $('wire').setAttribute('aria-pressed', on);
  render();
}

function setGrid(on) {
  if (grid) grid.visible = on;
  if (bedMesh) bedMesh.visible = on && camera.position.y > 0;
  $('grid').setAttribute('aria-pressed', on);
  render();
}

// ---- parts legend: one row per mesh node (colour swatch, name, visibility toggle)
function partMeshes() { const out = []; root.traverse(o => { if (o.isMesh) out.push(o); }); return out; }
function visibleBox() {
  const b = new T.Box3();
  partMeshes().forEach(m => { if (m.visible) b.union(new T.Box3().setFromObject(m)); });
  return b.isEmpty() ? new T.Box3().setFromObject(root) : b;
}
function buildParts() {
  const meshes = partMeshes();
  meshes.forEach(m => { if (m.userData.visible === false) m.visible = false; });
  const hidden = meshes.some(m => m.userData.visible === false);
  if (meshes.length < 2 && !hidden) return;
  const list = $('plist'), objs = new Map();
  meshes.forEach(m => { const o = m.userData.object || ''; if (!objs.has(o)) objs.set(o, []); objs.get(o).push(m); });
  const grouped = objs.size > 1;
  objs.forEach((ms, oname) => {
    if (grouped) { const h = document.createElement('div'); h.className = 'o'; h.textContent = oname; list.append(h); }
    ms.forEach(m => {
      const u = m.userData, lab = document.createElement('label'), cb = document.createElement('input');
      cb.type = 'checkbox'; cb.checked = m.visible;
      const sw = document.createElement('span'); sw.className = 'sw'; sw.style.background = u.color || '#999';
      const t = document.createElement('span');
      t.textContent = (u.name || m.name) + (u.subtype ? ' (' + u.subtype.replace(/_/g, ' ') + ')' : '') +
        (u.extruder ? '  \u00b7 filament ' + u.extruder : '');
      lab.append(cb, sw, t); list.append(lab);
      cb.onchange = () => { m.visible = cb.checked; if (boxHelper) buildScale(); render(); };
    });
  });
  $('parts').hidden = false;
}

function fallback(reason) {
  if (D.turntable && window.tuldaTurntable) { window.tuldaTurntable.show(reason); return true; }
  if (D.turntable) { say(reason + ' The turntable script did not load.', true); return false; }
  say(reason + (D.turntable ? '' : ' No turntable was rendered for this model (use --views turntable).'), true);
  return false;
}

async function main() {
  applyThemeEarly();
  $('theme').onclick = () => { dark = !dark; store.set('tulda.viewer.dark', dark ? '1' : '0'); applyTheme(); };
  if (!D.glb && D.turntable) { window.tuldaTurntable.show(''); return; }   // turntable-only render
  if (!hasWebGL()) { fallback('WebGL is not available in this browser.'); return; }
  say('Loading viewer...');
  let OrbitControls, GLTFLoader;
  try {
    T = await import('three');
    ({ OrbitControls } = await import(D.vendor + 'examples/jsm/controls/OrbitControls.js'));
    ({ GLTFLoader } = await import(D.vendor + 'examples/jsm/loaders/GLTFLoader.js'));
    renderer = new T.WebGLRenderer({ canvas: $('cv'), antialias: true, alpha: false });
  } catch (e) { fallback('The 3D viewer could not start (' + (e && e.message || e) + ').'); return; }
  $('cv').addEventListener('webglcontextlost', ev => { ev.preventDefault(); fallback('The graphics context was lost.'); });

  scene = new T.Scene();
  camera = new T.PerspectiveCamera(40, 1, 0.1, 10000);
  scene.add(new T.HemisphereLight(0xffffff, 0x555560, 1.5));
  const k = new T.DirectionalLight(0xffffff, 2.2); k.position.set(1, 2, 1.5); scene.add(k);
  const f = new T.DirectionalLight(0xffffff, 0.9); f.position.set(-2, -1, -1); scene.add(f);
  const b = new T.DirectionalLight(0xffffff, 0.8); b.position.set(0, -2, 1); scene.add(b);
  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = false;           // on-demand rendering: no animation loop
  controls.screenSpacePanning = true;
  controls.minPolarAngle = 0; controls.maxPolarAngle = Math.PI;   // all the way under
  controls.addEventListener('change', () => {
    if (bedMesh) bedMesh.visible = !!(grid && grid.visible) && camera.position.y > 0;   // do not veil the underside
    render();
  });
  applyTheme();

  let gltf;
  try {
    say('Loading model...');
    gltf = await new GLTFLoader().loadAsync(D.glb);
  } catch (e) { fallback('The model could not be loaded (' + (e && e.message || e) + ').'); return; }
  root = gltf.scene;
  scene.add(root);
  root.updateMatrixWorld(true);
  buildParts();
  bbox = visibleBox();
  if (bbox.isEmpty()) { say('The model is empty.', true); return; }
  center = bbox.getCenter(new T.Vector3());
  radius = bbox.getBoundingSphere(new T.Sphere()).radius || 1;
  buildGrid();
  buildScale();
  say('');
  new ResizeObserver(size).observe(stage);
  size();
  preset('iso');
  $('ui').hidden = false;
  document.querySelectorAll('[data-preset]').forEach(b => { b.onclick = () => preset(b.dataset.preset); });
  $('reset').onclick = () => preset('iso');
  $('wire').onclick = () => setWire(!wire);
  $('grid').onclick = () => setGrid(!(grid && grid.visible));
  $('scale').onclick = () => setScale(!scaleOn);
  const fs = $('full');
  if (stage.requestFullscreen || stage.webkitRequestFullscreen) {
    fs.onclick = () => { document.fullscreenElement ? document.exitFullscreen() : (stage.requestFullscreen || stage.webkitRequestFullscreen).call(stage); };
  } else fs.hidden = true;
  addEventListener('keydown', e => {
    if (e.target.closest && e.target.closest('input,button,summary')) return;
    if (e.key === 'r') preset('iso'); else if (e.key === 'w') setWire(!wire);
    else if (e.key === 'g') setGrid(!(grid && grid.visible));
    else if (e.key === 's') setScale(!scaleOn);
  });
  window.tuldaReady = true;
}
function applyThemeEarly() { document.documentElement.dataset.theme = dark ? 'dark' : 'light'; $('theme').textContent = dark ? 'Light background' : 'Dark background'; }
main().catch(e => { console.error(e); fallback('The viewer failed: ' + (e && e.message || e)); });
