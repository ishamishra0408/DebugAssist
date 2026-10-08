/* DebugAssistAgent · advisor identities.
   Each advisor card carries a small living scene drawn by its own WebGL shader (raw WebGL, no library): a field of
   strands in the advisor's palette, drifting motes, a scan-light that reveals it on entrance, strands that part
   around the pointer. It is calm while idle, quickens and brightens while the advisor is being asked, and settles
   with a glow when it answers. Inspired by ThreeUI's Sylva "Living Green" (procedural, pointer-led, adaptive motion);
   written from scratch. GSAP (when it loads) staggers the cards in and tilts them under the pointer.
   Reduce Motion: one still frame, no tilt, no stagger. Hidden or off-screen: the loop stops. */
(() => {
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const PALETTES = {           // a, b: the two inks; motif 0 = flowing strands … 1 = rings around a lens
    tide:   { a: [0.30, 0.62, 1.0], b: [0.62, 0.86, 1.0], motif: 0 },    // defect-triage: currents to sort
    violet: { a: [0.64, 0.46, 1.0], b: [0.92, 0.62, 1.0], motif: 1 },    // cause-locator: rings closing on a point
    ember:  { a: [1.0, 0.58, 0.24], b: [1.0, 0.36, 0.42], motif: 0.6 },  // allspaw: incident review
    moss:   { a: [0.30, 0.86, 0.52], b: [0.62, 0.98, 0.80], motif: 0 },  // qe-ic-advisor: quality gate
  };
  const FRAG = `
precision highp float;
uniform vec2 uRes; uniform float uTime, uEnergy, uScan, uMotif, uDark, uGlow; uniform vec2 uPtr; uniform float uPtrOn;
uniform vec3 uA, uB, uBg;
float hash(vec2 p){ return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
float noise(vec2 p){ vec2 i = floor(p), f = fract(p); vec2 u = f * f * (3. - 2. * f);
  return mix(mix(hash(i), hash(i + vec2(1, 0)), u.x), mix(hash(i + vec2(0, 1)), hash(i + vec2(1, 1)), u.x), u.y); }
float fbm(vec2 p){ float v = 0., a = .5; for (int i = 0; i < 5; i++) { v += a * noise(p); p *= 2.03; a *= .5; } return v; }
void main(){
  vec2 uv = gl_FragCoord.xy / uRes;
  vec2 p = (gl_FragCoord.xy - .5 * uRes) / uRes.y;
  float t = uTime * (.05 + .22 * uEnergy);
  vec2 d = p - uPtr; float r = length(d);
  p += uPtrOn * normalize(d + 1e-4) * .10 * exp(-r * r * 16.);                 // strands part around the pointer
  vec2 q = vec2(fbm(p * 1.5 + t), fbm(p * 1.5 - t + 3.1));
  vec2 w = p + 1.35 * q;
  float f = fbm(w * 2. + vec2(0., t * 1.4));
  float lv = mix(f * 13., length(w - vec2(.35, .05)) * 10. + f * 5., uMotif);   // strands, or rings around a lens
  float strand = 1. - smoothstep(0., .07 + .06 * uEnergy, abs(fract(lv) - .5));
  vec3 ink = mix(uA, uB, clamp(q.x * 1.2, 0., 1.));
  float amount = .18 + .35 * f + strand * (.30 + .45 * uEnergy) + .25 * uGlow;
  vec3 col = mix(uBg, ink, clamp(amount * (uDark > .5 ? 1. : .8), 0., 1.));
  vec2 g = uv * vec2(uRes.x / uRes.y, 1.) * 10.; g.y -= uTime * .05 * (1. + 2.5 * uEnergy);  // drifting motes
  vec2 gi = floor(g), gf = fract(g) - .5; float h = hash(gi);
  vec2 off = (vec2(hash(gi + 1.3), hash(gi + 2.7)) - .5) * .6;
  float mote = step(.84, h) * smoothstep(.07, 0., length(gf - off)) * (.55 + .45 * sin(uTime * 2. + h * 20.));
  col = mix(col, uB, mote * .8);
  float shown = smoothstep(0., .03, uScan - uv.x);                             // the scan-light reveal
  float edge = exp(-pow((uv.x - uScan) * 38., 2.)) * step(uScan, 1.02);
  col = mix(uBg, col, shown) + edge * uB * .55;
  col = mix(col, uBg, smoothstep(.62, 0., uv.y) * .62);                        // room for the name, both themes
  gl_FragColor = vec4(col, 1.);
}`;
  const VERT = "attribute vec2 a; void main(){ gl_Position = vec4(a, 0., 1.); }";

  function dark() {
    const t = document.documentElement.dataset.theme;
    return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  }

  function sigil(canvas) {
    const gl = canvas.getContext("webgl", { antialias: false, premultipliedAlpha: false });
    if (!gl) { canvas.classList.add("no-gl"); return null; }
    const sh = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s; };
    const prog = gl.createProgram();
    gl.attachShader(prog, sh(gl.VERTEX_SHADER, VERT)); gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, FRAG));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) { canvas.classList.add("no-gl"); return null; }
    gl.useProgram(prog);
    const buf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    const loc = gl.getAttribLocation(prog, "a"); gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    const U = n => gl.getUniformLocation(prog, n);
    const pal = PALETTES[canvas.dataset.palette] || PALETTES.tide;
    const s = { canvas, gl, U, pal, energy: 0, glow: 0, scan: still ? 1.1 : 0, ptr: [0, 0], ptrOn: 0, t0: performance.now(),
                seed: Math.random() * 40, visible: true };
    const size = () => {
      const r = canvas.getBoundingClientRect(), dpr = Math.min(2, devicePixelRatio || 1);
      canvas.width = Math.max(1, Math.round(r.width * dpr)); canvas.height = Math.max(1, Math.round(r.height * dpr));
      gl.viewport(0, 0, canvas.width, canvas.height);
    };
    size(); new ResizeObserver(() => { size(); if (still) draw(s); }).observe(canvas);
    const card = canvas.closest(".adv-tile, .adv-card");
    card.addEventListener("pointermove", ev => {
      const r = canvas.getBoundingClientRect();
      s.ptr = [((ev.clientX - r.left) - r.width / 2) / r.height, (r.height / 2 - (ev.clientY - r.top)) / r.height];
      s.ptrOn = 1;
    });
    card.addEventListener("pointerleave", () => { s.ptrOn = 0; });
    return s;
  }

  function draw(s) {
    const { gl, U, pal, canvas } = s, bg = dark() ? [0.07, 0.075, 0.085] : [0.965, 0.968, 0.975];
    gl.uniform2f(U("uRes"), canvas.width, canvas.height);
    gl.uniform1f(U("uTime"), still ? 7 + s.seed : (performance.now() - s.t0) / 1000 + s.seed);
    gl.uniform1f(U("uEnergy"), s.energy); gl.uniform1f(U("uGlow"), s.glow); gl.uniform1f(U("uScan"), s.scan);
    gl.uniform1f(U("uMotif"), pal.motif); gl.uniform1f(U("uDark"), dark() ? 1 : 0);
    gl.uniform2f(U("uPtr"), s.ptr[0], s.ptr[1]); gl.uniform1f(U("uPtrOn"), still ? 0 : s.ptrOn);
    gl.uniform3fv(U("uA"), pal.a); gl.uniform3fv(U("uB"), pal.b); gl.uniform3fv(U("uBg"), bg);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  const scenes = [...document.querySelectorAll("canvas.sigil")].map(sigil).filter(Boolean);
  const bySeat = seat => scenes.find(s => s.canvas.dataset.seat === seat);
  if (!scenes.length) return;

  // the loop runs only while a scene is on screen and the page is visible
  const io = new IntersectionObserver(es => es.forEach(e => {
    const s = scenes.find(x => x.canvas === e.target); if (s) s.visible = e.isIntersecting;
  }));
  scenes.forEach(s => io.observe(s.canvas));
  const ease = (from, to, k) => from + (to - from) * k;
  function frame() {
    if (!document.hidden) scenes.forEach(s => {
      if (!s.visible) return;
      s.energy = ease(s.energy, s.targetEnergy || 0, .06); s.glow = ease(s.glow, 0, .03);
      draw(s);
    });
    requestAnimationFrame(frame);
  }
  if (still) scenes.forEach(draw); else requestAnimationFrame(frame);

  // the asking flow (the page's own script announces it): quicken while asked, glow when answered
  document.addEventListener("advisor-state", ev => {
    const s = bySeat(ev.detail.seat); if (!s) return;
    s.targetEnergy = ev.detail.state === "asking" ? 1 : 0;
    if (ev.detail.state === "answered") s.glow = 1;
    if (still) { s.energy = s.targetEnergy; draw(s); }
  });

  // entrance and tilt: GSAP when it is there (and motion is welcome), plain otherwise
  const cards = [...document.querySelectorAll(".adv-tile, .adv-card")];
  if (still || !window.gsap) { scenes.forEach(s => { s.scan = 1.1; }); return; }
  gsap.from(cards, { y: 28, opacity: 0, duration: .9, ease: "power3.out", stagger: .14, clearProps: "transform,opacity" });
  scenes.forEach((s, i) => gsap.to(s, { scan: 1.1, duration: 1.4, ease: "power2.inOut", delay: .25 + i * .14 }));
  cards.forEach(card => {
    card.addEventListener("pointermove", ev => {
      if (ev.target.closest("input, textarea, button")) return;
      const r = card.getBoundingClientRect();
      const x = (ev.clientX - r.left) / r.width - .5, y = (ev.clientY - r.top) / r.height - .5;
      gsap.to(card, { rotateY: x * 6, rotateX: -y * 5, duration: .5, ease: "power2.out", transformPerspective: 700 });
    });
    card.addEventListener("pointerleave", () => gsap.to(card, { rotateY: 0, rotateX: 0, duration: .7, ease: "power3.out" }));
  });
})();
