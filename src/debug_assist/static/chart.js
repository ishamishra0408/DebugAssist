/* The state chart's dot. "This run" walks the run's real path and stays where the run is; "Every path" loops a tour.
   When the page updates in place, steps already walked are drawn at once and only new ones animate. Click to pause.
   Under Reduce Motion the path is drawn without a moving dot. */
window.DA_chart = (function () {
  let gen = 0;
  const walked = {};  // per mode: how many steps were already animated (so a refresh doesn't replay them)
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  return function start() {
    const g = ++gen;
    document.querySelectorAll(".chart").forEach(ch => {
      const paths = JSON.parse(ch.dataset.paths || "{}");
      const svg = ch.querySelector("svg"), tok = svg.querySelector(".ctok"), cap = ch.querySelector(".chart-cap");
      const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
      let mode = ch.dataset.start, paused = false, run = 0;
      svg.addEventListener("click", () => { paused = !paused; ch.classList.toggle("paused", paused); });
      ch.querySelectorAll('input[name="cm"]').forEach(r => r.addEventListener("change", () => {
        mode = r.value; ch.dataset.start = mode; walked[mode] = 0; go();
      }));
      const reset = () => {
        svg.querySelectorAll(".cn, .cpill").forEach(n => n.classList.remove("on", "seen"));
        svg.querySelectorAll(".ce").forEach(e => e.classList.remove("hot", "seen"));
      };
      const node = id => svg.querySelector(id.startsWith("stop-") ? "#cp-" + id.slice(5) : "#cn-" + id);
      const land = id => {
        svg.querySelectorAll(".cn.on, .cpill.on").forEach(n => n.classList.remove("on"));
        const n = node(id); if (n) n.classList.add("on", "seen");
        const r = n && n.querySelector("rect");
        if (r) { tok.setAttribute("cx", +r.getAttribute("x")); tok.setAttribute("cy", +r.getAttribute("y") + +r.getAttribute("height") / 2); }  // on the box's edge, clear of its name
      };
      const mark = eid => { const p = svg.querySelector("#" + eid); if (p) p.classList.add("seen"); };
      const fly = (eid, dur, me) => new Promise(res => {
        const p = svg.querySelector("#" + eid);
        if (!p) return res();
        p.classList.add("hot");
        const len = p.getTotalLength(), t0 = performance.now();
        (function fr(now) {
          if (g !== gen || me !== run) return res();
          if (paused) { requestAnimationFrame(fr); return; }
          const k = Math.min(1, (now - t0) / dur), pt = p.getPointAtLength(len * k);
          tok.setAttribute("cx", pt.x); tok.setAttribute("cy", pt.y);
          if (k < 1) requestAnimationFrame(fr); else { p.classList.remove("hot"); p.classList.add("seen"); res(); }
        })(t0);
      });
      async function go() {
        const me = ++run, steps = paths[mode] || [];
        reset(); land("issue"); tok.style.display = still ? "none" : "";
        if (!steps.length) { cap.textContent = "Nothing has happened yet."; return; }
        const skip = still ? steps.length : Math.min(walked[mode] || 0, steps.length);
        for (let i = 0; i < skip; i++) { mark(steps[i][0]); land(steps[i][1]); }
        if (skip) cap.textContent = steps[skip - 1][2];
        for (let i = skip; i < steps.length; i++) {
          while (paused) { await sleep(200); if (g !== gen || me !== run) return; }
          if (g !== gen || me !== run) return;
          cap.textContent = steps[i][2];
          await fly(steps[i][0], 850, me);
          if (g !== gen || me !== run) return;
          land(steps[i][1]); walked[mode] = i + 1;
          await sleep(1300);
        }
        if (mode === "all" && !still) { await sleep(2200); if (g === gen && me === run) { walked.all = 0; go(); } }
      }
      go();
    });
  };
})();
document.addEventListener("DOMContentLoaded", () => window.DA_chart());
