/*
 * Background: slow topographic contour lines over a faint grid, drawn with raw WebGL. The lines behave like water under
 * the pointer: moving it sends out ripples that spread and fade, hovering swells the lines beneath it like a lens, and
 * a click sends a stronger ripple. Under Reduce Motion there are no ripples and the drift stops.
 * Ported from ThreeUI's "Topo Field" (https://github.com/MengTo/threeui, src/shaders/neuform-isolated/sources/topo-field.html)
 * and changed for a work screen: dimmer lines and grid, slower drift, light and dark ink, 30 fps, paused when the tab is
 * hidden, one still frame under Reduce Motion, nothing at all if WebGL is missing.
 *
 * MIT License. Copyright (c) 2026 Meng To.
 * Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
 * documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
 * rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit
 * persons to whom the Software is furnished to do so, subject to the following conditions: The above copyright notice
 * and this permission notice shall be included in all copies or substantial portions of the Software.
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
 * WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
 * COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
 * OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
 */
(function () {
  const canvas = document.getElementById("topo");
  if (!canvas) return;
  const gl = canvas.getContext("webgl", { alpha: true, antialias: false, depth: false, premultipliedAlpha: false });
  if (!gl) { canvas.remove(); return; }

  const vs = "attribute vec2 p; void main(){ gl_Position = vec4(p, 0.0, 1.0); }";
  const fs = `
    precision highp float;
    uniform vec2 u_res; uniform float u_time; uniform float u_dpr; uniform vec3 u_ink; uniform float u_gridA; uniform float u_lineA;
    uniform vec4 u_drops[12];   // ripples: x, y (canvas px), start time (s), strength
    uniform vec3 u_mouse;       // x, y (canvas px), hover strength 0..1
    vec3 permute(vec3 x){ return mod(((x*34.0)+1.0)*x, 289.0); }
    float snoise(vec2 v){
      const vec4 C = vec4(0.211324865405187, 0.366025403784439, -0.577350269189626, 0.024390243902439);
      vec2 i = floor(v + dot(v, C.yy)); vec2 x0 = v - i + dot(i, C.xx);
      vec2 i1 = (x0.x > x0.y) ? vec2(1.0, 0.0) : vec2(0.0, 1.0);
      vec4 x12 = x0.xyxy + C.xxzz; x12.xy -= i1; i = mod(i, 289.0);
      vec3 p = permute(permute(i.y + vec3(0.0, i1.y, 1.0)) + i.x + vec3(0.0, i1.x, 1.0));
      vec3 m = max(0.5 - vec3(dot(x0,x0), dot(x12.xy,x12.xy), dot(x12.zw,x12.zw)), 0.0); m = m*m; m = m*m;
      vec3 x = 2.0 * fract(p * C.www) - 1.0; vec3 h = abs(x) - 0.5; vec3 ox = floor(x + 0.5); vec3 a0 = x - ox;
      m *= 1.79284291400159 - 0.85373472095314 * (a0*a0 + h*h);
      vec3 g; g.x = a0.x * x0.x + h.x * x0.y; g.yz = a0.yz * x12.xz + h.yz * x12.yw;
      return 130.0 * dot(m, g);
    }
    void main(){
      vec2 frag = gl_FragCoord.xy; float crest = 0.0;
      for (int k = 0; k < 12; k++) {                       // water: rings that travel out from each drop and fade
        vec4 dr = u_drops[k]; float age = u_time - dr.z;
        if (dr.w <= 0.0 || age <= 0.0 || age > 3.2) continue;
        vec2 dv = gl_FragCoord.xy - dr.xy; float d = length(dv) / u_dpr;
        float ring = d - age * 230.0;
        float w = sin(ring * 0.075) * exp(-ring * ring / 2600.0) * exp(-age * 1.5) * dr.w;
        frag += (dv / max(length(dv), 1.0)) * w * 11.0 * u_dpr; crest += abs(w);
      }
      vec2 mv = gl_FragCoord.xy - u_mouse.xy; float md = length(mv) / u_dpr;
      float lens = exp(-md * md / (2.0 * 110.0 * 110.0)) * u_mouse.z;   // a drop of water resting under the pointer
      frag -= mv * lens * 0.22;
      vec2 st = frag / u_res.xy; st.x *= u_res.x / u_res.y;
      float gs = 56.0 * u_dpr; vec2 gf = fract(frag / gs); float t = 1.0 / gs;
      float grid = clamp(step(1.0 - t, gf.x) + step(1.0 - t, gf.y), 0.0, 1.0) * u_gridA;
      float n = snoise(st * 1.25 + vec2(u_time * 0.010, u_time * 0.016)) * 0.5 + 0.5;
      float tri = abs(fract(n * 9.0) - 0.5) * 2.0;
      float lines = smoothstep(0.03, 0.0, tri) * u_lineA * (1.0 + lens * 1.1 + min(crest, 1.0) * 0.9);
      gl_FragColor = vec4(u_ink, clamp(grid + lines, 0.0, 1.0));
    }`;
  const sh = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s; };
  const prog = gl.createProgram();
  gl.attachShader(prog, sh(gl.VERTEX_SHADER, vs)); gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, fs));
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) { canvas.remove(); return; }
  gl.useProgram(prog);
  gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
  const loc = gl.getAttribLocation(prog, "p");
  gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
  const u = n => gl.getUniformLocation(prog, n);
  const uRes = u("u_res"), uTime = u("u_time"), uDpr = u("u_dpr"), uInk = u("u_ink"), uGrid = u("u_gridA"), uLine = u("u_lineA");
  const uDrops = u("u_drops"), uMouse = u("u_mouse");
  const drops = new Float32Array(48); let next = 0;        // 12 ripples × (x, y, start, strength)
  const pointer = { x: -1e4, y: -1e4, hover: 0, want: 0, lastX: -1e4, lastY: -1e4, lastT: 0, busyUntil: 0 };

  const dark = matchMedia("(prefers-color-scheme: dark)");
  const still = matchMedia("(prefers-reduced-motion: reduce)");
  const ink = () => {
    if (dark.matches) { gl.uniform3f(uInk, 1, 1, 1); gl.uniform1f(uGrid, 0.05); gl.uniform1f(uLine, 0.26); }
    else { gl.uniform3f(uInk, 0.11, 0.11, 0.13); gl.uniform1f(uGrid, 0.055); gl.uniform1f(uLine, 0.18); }
  };
  const size = () => {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(innerWidth * dpr); canvas.height = Math.round(innerHeight * dpr);
    gl.viewport(0, 0, canvas.width, canvas.height); gl.uniform2f(uRes, canvas.width, canvas.height); gl.uniform1f(uDpr, dpr);
  };
  const t0 = performance.now(); let last = 0, raf = 0;
  const secs = now => (now - t0) / 1000 + 40.0;
  const draw = now => {
    pointer.hover += (pointer.want - pointer.hover) * 0.12;   // the lens eases in and out
    gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);
    gl.uniform1f(uTime, secs(now)); gl.uniform4fv(uDrops, drops);
    gl.uniform3f(uMouse, pointer.x, pointer.y, pointer.hover); gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  };
  const loop = now => {
    raf = requestAnimationFrame(loop);
    const busy = now < pointer.busyUntil || Math.abs(pointer.want - pointer.hover) > 0.01;
    if (now - last < (busy ? 15 : 33)) return;  // 60 fps while water moves, 30 for the slow drift
    last = now; draw(now);
  };
  const drop = (cx, cy, strength) => {
    const dpr = Math.min(window.devicePixelRatio || 1, 2), now = performance.now();
    drops.set([cx * dpr, (innerHeight - cy) * dpr, secs(now), strength], next * 4); next = (next + 1) % 12;
    pointer.busyUntil = now + 3300;
  };
  addEventListener("pointermove", ev => {
    if (still.matches) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2), now = performance.now();
    pointer.x = ev.clientX * dpr; pointer.y = (innerHeight - ev.clientY) * dpr; pointer.want = 1;
    const dist = Math.hypot(ev.clientX - pointer.lastX, ev.clientY - pointer.lastY);
    if (dist > 70 || (dist > 12 && now - pointer.lastT > 140)) {
      drop(ev.clientX, ev.clientY, Math.min(1, 0.35 + dist / 260));
      pointer.lastX = ev.clientX; pointer.lastY = ev.clientY; pointer.lastT = now;
    }
  }, { passive: true });
  addEventListener("pointerdown", ev => { if (!still.matches) drop(ev.clientX, ev.clientY, 1.4); }, { passive: true });
  document.documentElement.addEventListener("pointerleave", () => { pointer.want = 0; });
  addEventListener("blur", () => { pointer.want = 0; });
  const start = () => { cancelAnimationFrame(raf); if (still.matches || document.hidden) draw(performance.now()); else raf = requestAnimationFrame(loop); };
  ink(); size(); start();
  addEventListener("resize", () => { size(); draw(performance.now()); });
  dark.addEventListener("change", () => { ink(); draw(performance.now()); });
  still.addEventListener("change", start);
  document.addEventListener("visibilitychange", start);
})();
