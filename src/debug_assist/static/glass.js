/* Liquid Glass reacts to touch: a soft highlight follows the pointer across a glass control, the way light moves
   across Apple's material. Glass controls only; nothing under Reduce Motion. */
(function () {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  let cur = null;
  const off = () => { if (cur) cur.style.setProperty("--gl", "0"); cur = null; };
  addEventListener("pointermove", ev => {
    const g = ev.target.closest ? ev.target.closest(".glass") : null;
    if (cur && cur !== g) off();
    if (!g) return;
    cur = g;
    const r = g.getBoundingClientRect();
    g.style.setProperty("--gx", ((ev.clientX - r.left) / r.width * 100).toFixed(1) + "%");
    g.style.setProperty("--gy", ((ev.clientY - r.top) / r.height * 100).toFixed(1) + "%");
    g.style.setProperty("--gl", "1");
  }, { passive: true });
  document.documentElement.addEventListener("pointerleave", off);
})();
