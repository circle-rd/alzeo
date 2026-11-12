// The cockpit's backdrop: each theme's ground, and two soft halos of light breathing behind
// every view. In indigo they are violet; graphite and paper keep the same moving light in
// neutral greys.
//
// One full-window canvas under the page, drawn by a small WebGL shader: a halo low in the
// middle and a deeper one high on the right, each a wide gaussian of light drifting a little,
// slowly, over the theme's ground. It is decoration and is treated as such: drawn at a quarter
// of the window's width and height and stretched (the light is smooth, nothing is lost), 8
// frames a second at most (the halos move a few pixels a second), paused in a hidden tab and
// under the Agent's fullscreen frame, one still frame for a system that asks for reduced
// motion, and gone without a trace where WebGL is missing or the system forces its own colours
// (the same light in CSS, under it, is the fallback). Nothing here touches the box.
(function(){
  const canvas = document.getElementById('backdrop');
  if (!canvas) return;
  const forced = window.matchMedia && window.matchMedia('(forced-colors: active)').matches;
  const gl = forced ? null : canvas.getContext('webgl', {alpha: false, antialias: false, depth: false, stencil: false,
                                                          powerPreference: 'low-power', preserveDrawingBuffer: false});
  if (!gl){ canvas.remove(); return; }

  const VERT = 'attribute vec2 p; void main(){ gl_Position = vec4(p, 0.0, 1.0); }';
  // The ground and the two halo colours come from the theme's tokens (--backdrop-*), so the
  // same shader carries indigo's violet and the neutral greys of graphite and paper. The
  // centres move here, in double precision: a clock in the shader's mediump float
  // loses its seconds within the hour on a phone, and stops the drawing within the day.
  const FRAG = `
precision mediump float;
uniform vec2 res, a, b;
uniform vec3 uGround, uColA, uColB;
uniform float uSa, uSb;
const float ra = 0.95, rb = 0.70;
void main(){
  vec2 uv = gl_FragCoord.xy / res;
  vec2 p = vec2(uv.x * res.x / res.y, uv.y);
  float ga = exp(-pow(distance(p, a) / ra, 2.0)), gb = exp(-pow(distance(p, b) / rb, 2.0));
  gl_FragColor = vec4(uGround + uColA * ga * uSa + uColB * gb * uSb, 1.0);
}`;

  function compile(type, src){
    const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
    return gl.getShaderParameter(s, gl.COMPILE_STATUS) ? s : null;
  }
  const vs = compile(gl.VERTEX_SHADER, VERT), fs = compile(gl.FRAGMENT_SHADER, FRAG);
  if (!vs || !fs){ canvas.remove(); return; }
  const prog = gl.createProgram(); gl.attachShader(prog, vs); gl.attachShader(prog, fs); gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)){ canvas.remove(); return; }
  gl.useProgram(prog);
  const buf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
  const loc = gl.getAttribLocation(prog, 'p'); gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  const uRes = gl.getUniformLocation(prog, 'res'), uCa = gl.getUniformLocation(prog, 'a'), uCb = gl.getUniformLocation(prog, 'b');
  const uGround = gl.getUniformLocation(prog, 'uGround'), uColA = gl.getUniformLocation(prog, 'uColA'), uColB = gl.getUniformLocation(prog, 'uColB');
  const uSa = gl.getUniformLocation(prog, 'uSa'), uSb = gl.getUniformLocation(prog, 'uSb');
  // indigo's shader values, used when the stylesheet has not given the tokens (a test harness,
  // or a page whose CSS has not loaded): the fallback is the theme the cockpit shipped with.
  const FALLBACK_GROUND = [0.090, 0.043, 0.220];
  const FALLBACK_A = [0.46, 0.20, 0.92, 0.50], FALLBACK_B = [0.40, 0.17, 0.80, 0.26];
  function colour(value, fallback){
    const s = (value || '').trim();
    let m = /^#([0-9a-f]{6})$/i.exec(s);
    if (m){ const n = parseInt(m[1], 16); return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255, 1]; }
    m = /^#([0-9a-f]{3})$/i.exec(s);
    if (m){ const n = parseInt(m[1], 16); return [((n >> 8) & 15) / 15, ((n >> 4) & 15) / 15, (n & 15) / 15, 1]; }
    m = /^rgba?\(([^)]+)\)$/i.exec(s);
    if (m){
      const p = m[1].split(',').map(x => parseFloat(x.trim()));
      if (p.length >= 3 && p.every(x => Number.isFinite(x))) return [p[0] / 255, p[1] / 255, p[2] / 255, p.length > 3 ? p[3] : 1];
    }
    return fallback;
  }
  function paint(){
    const cs = window.getComputedStyle ? window.getComputedStyle(document.documentElement) : null;
    const ground = colour(cs && cs.getPropertyValue('--backdrop-ground'), FALLBACK_GROUND);
    const a = colour(cs && cs.getPropertyValue('--backdrop-a'), FALLBACK_A);
    const b = colour(cs && cs.getPropertyValue('--backdrop-b'), FALLBACK_B);
    gl.uniform3f(uGround, ground[0], ground[1], ground[2]);
    gl.uniform3f(uColA, a[0], a[1], a[2]); gl.uniform1f(uSa, a[3]);
    gl.uniform3f(uColB, b[0], b[1], b[2]); gl.uniform1f(uSb, b[3]);
  }

  const still = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)');
  const SCALE = () => (Math.min(window.innerWidth, window.innerHeight) < 700 ? 0.2 : 0.25);
  let w = 0, h = 0, raf = 0, last = -Infinity, t0 = performance.now(), dead = false;
  function size(){
    const k = SCALE();
    w = Math.max(160, Math.round(window.innerWidth * k)); h = Math.max(120, Math.round(window.innerHeight * k));
    if (canvas.width !== w || canvas.height !== h){ canvas.width = w; canvas.height = h; gl.viewport(0, 0, w, h); }
  }
  function draw(now){
    const tt = (now - t0) / 1000 * 0.03, k = w / h;
    gl.uniform2f(uRes, w, h);
    gl.uniform2f(uCa, (0.55 + 0.06 * Math.sin(tt * 1.3)) * k, 0.06 + 0.06 * Math.cos(tt));
    gl.uniform2f(uCb, (0.96 + 0.05 * Math.cos(tt * 0.9)) * k, 0.98 + 0.06 * Math.sin(tt * 1.1));
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }
  const covered = () => document.body && document.body.classList.contains('agentmax');
  // Under reduced motion the one still frame is always drawn, covered or not: it is the only
  // one. It went through the 8 fps gate, and a first frame earlier than 125 ms after the page
  // started was skipped and never asked for again, the ground left black (3 loads in 4, v1.27.0).
  function frame(now){
    raf = 0;
    if (document.hidden) return;
    const once = !!(still && still.matches);
    if (once || (now - last >= 1000 / 8 && !covered())){ last = now; draw(now); }
    if (!once) raf = requestAnimationFrame(frame);
  }
  function start(){ if (dead) return; size(); if (!raf) raf = requestAnimationFrame(frame); }
  function sync(){
    document.documentElement.classList.add('has-backdrop');
    paint();
    start();
  }
  window.cockpitBackdrop = {sync};
  // a new size clears the canvas: it is drawn again at once, not at the next frame's turn
  window.addEventListener('resize', () => { if (dead) return; size(); draw(performance.now()); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) start(); });
  if (still && still.addEventListener) still.addEventListener('change', start);
  // a lost context (the GPU reset, the tab evicted) leaves the CSS light, never a reload
  canvas.addEventListener('webglcontextlost', () => {
    dead = true; if (raf) cancelAnimationFrame(raf); raf = 0;
    canvas.remove(); document.documentElement.classList.remove('has-backdrop');
  });
  sync();
})();
