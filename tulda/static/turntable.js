// tulda turntable widget: drag (or arrow keys / slider) to rotate through pre-rendered PNG
// frames. Used as the no-WebGL fallback and for turntable-only renders. No dependencies.
(function () {
  const D = JSON.parse(document.getElementById('vdata').textContent);
  const T = D.turntable;
  const root = document.getElementById('tt');
  if (!T || !root) return;
  const n = T.frames;
  const url = i => T.base + T.pattern.replace('{i}', String(i).padStart(T.digits, '0'));
  let cur = 0, built = false;
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; };

  function build(reason) {
    built = true;
    const img = el('img'); img.alt = 'Turntable view. Drag left or right to rotate.'; img.draggable = false;
    img.tabIndex = 0; img.src = url(0);
    const bar = el('div', 'bar');
    const range = el('input'); range.type = 'range'; range.min = 0; range.max = n - 1; range.value = 0;
    range.setAttribute('aria-label', 'Rotation');
    const lab = el('span', '', '');
    const play = el('button', 'b', 'Auto-rotate'); play.type = 'button'; play.setAttribute('aria-pressed', 'false');
    bar.append(range, lab, play);
    root.append(img, bar);
    if (reason) root.append(el('div', 'note', reason + ' Showing pre-rendered turntable frames instead.'));
    const loaded = new Set();
    function go(i) {
      cur = ((Math.round(i) % n) + n) % n;
      img.src = url(cur); range.value = cur;
      lab.textContent = Math.round(cur * 360 / n) + '°';
      if (!loaded.has(cur)) loaded.add(cur);
    }
    // preload the rest after the first frame is in
    const preload = () => { for (let i = 1; i < n; i++) { const p = new Image(); p.src = url(i); } };
    img.addEventListener('load', preload, { once: true });
    go(0);
    // drag: one image width = one full turn
    let down = null;
    img.addEventListener('pointerdown', e => { down = { x: e.clientX, f: cur }; img.setPointerCapture(e.pointerId); stop(); });
    img.addEventListener('pointermove', e => {
      if (!down) return;
      const w = img.clientWidth || 300;
      go(down.f - (e.clientX - down.x) / w * n);
    });
    const up = () => { down = null; };
    img.addEventListener('pointerup', up); img.addEventListener('pointercancel', up);
    range.addEventListener('input', () => { stop(); go(+range.value); });
    img.addEventListener('keydown', e => {
      if (e.key === 'ArrowLeft') { stop(); go(cur - 1); e.preventDefault(); }
      else if (e.key === 'ArrowRight') { stop(); go(cur + 1); e.preventDefault(); }
    });
    let timer = null;
    function stop() { if (timer) { clearInterval(timer); timer = null; play.setAttribute('aria-pressed', 'false'); } }
    play.onclick = () => {
      if (timer) { stop(); return; }
      play.setAttribute('aria-pressed', 'true');
      timer = setInterval(() => go(cur + 1), 100);
    };
    window.tuldaTurntableReady = true;
  }

  window.tuldaTurntable = {
    show(reason) {
      document.getElementById('cv').hidden = true;
      ['msg', 'ui', 'parts'].forEach(id => { const e = document.getElementById(id); if (e) e.hidden = true; });
      root.hidden = false;
      if (!built) build(reason);
    },
  };
  window.dispatchEvent(new Event('tulda-turntable'));
})();
