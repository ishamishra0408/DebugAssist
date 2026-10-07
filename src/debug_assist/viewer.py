"""The run viewer: one read-only page per run, built from MongoDB (the run's checkpointed state, its event log and
its spend meter). Ruled 2026-10-07: read-only, approval stays in the terminal (the go-word is a guardrail; the page
has no button that acts, only one that copies the approve command).

  uv run debug-assist view <run-id> [--watch]     → runs/<run-id>/view.html (with --watch, rebuilt every 3 s and the
                                                     page reloads itself until the run pauses or stops)

Everything that came from outside (issue text, PR descriptions in the evidence, model-written story and PR text) is
escaped, and markdown is rendered in the browser through DOMPurify, so no text in a run can run script in the page.
"""
import html
import json
import re
from datetime import datetime
from pathlib import Path

STEPS = [("read_issue", "Read issue", "triage"), ("reproduce", "Reproduce", "repro"), ("find_cause", "Find cause", "cause"),
         ("write_fix", "Write fix", "fix"), ("why_it_shipped", "Why it shipped", "second_story"),
         ("lasting_guard", "Lasting guard", "guard"), ("test_past_bugs", "Past bugs", "backtest"),
         ("approval", "Your approval", "approval"), ("open_pr", "Open PR", "published")]
e = lambda x: html.escape(str(x if x is not None else ""))
GOOD_EXITS = {"READY FOR YOU TO PUBLISH"}  # a finished run, not a failure: shown green


def gather(run_id: str) -> dict:
    """Everything the page shows, read once. Separate from render() so tests can render without MongoDB."""
    from . import events, meter
    from .graph import build
    from .store import db
    app, cfg = build(), {"configurable": {"thread_id": run_id}}
    snap = app.get_state(cfg)
    intr = snap.tasks[0].interrupts[0].value if snap.tasks and snap.tasks[0].interrupts else {}
    pr_text, pr_ok = "", None
    if intr.get("pr_body_path") and Path(intr["pr_body_path"]).exists():
        from .guardrails import fingerprint
        pr_text = Path(intr["pr_body_path"]).read_text()
        pr_ok = fingerprint(pr_text) == intr.get("sha256")
    patch = ""
    pp = ((snap.values or {}).get("fix") or {}).get("patch_path")
    if pp and Path(pp).exists():
        patch = Path(pp).read_text()
    calls = list(db()["calls"].find({"run_id": run_id}, {"_id": 0}).sort("at", 1))
    return {"run_id": run_id, "state": snap.values or {}, "next": list(snap.next or []), "interrupt": intr,
            "pr_text": pr_text, "pr_matches": pr_ok, "patch": patch, "events": events.for_run(run_id),
            "trials": events.trials_of(run_id), "meter": meter.snapshot(run_id) or {}, "calls": calls,
            "built": datetime.now().strftime("%H:%M:%S"), "now": datetime.now().astimezone().isoformat()}


def step_rows(d: dict) -> list[dict]:
    s, out = d["state"], []
    stop = (s.get("outcome") or {})
    logs = s.get("log", [])
    stopped_at = None
    for i, (key, label, field) in enumerate(STEPS):
        lines = [l.split(":", 1)[1].strip() for l in logs if l.startswith(key + ":")]
        if stop and lines and any("STOPPED" in l for l in lines):
            stopped_at = i
        if field in s and s.get(field) not in (None, {}):
            status = "done"
        elif key in d["next"]:
            status = "waiting for you" if d["interrupt"] else "next"
        else:
            status = "not reached"
        if stopped_at == i:
            status = "stopped"
        ev = [x for x in d["events"] if x.get("step") == key]
        span = f"{ev[0]['at'][11:19]}–{ev[-1]['at'][11:19]}" if ev else ""
        out.append({"key": key, "label": label, "status": status, "lines": lines, "span": span, "events": len(ev)})
    if stopped_at is not None:
        for r in out[stopped_at + 1:]:
            r["status"] = "not reached"
    return out


def _diff(patch: str) -> str:
    rows = []
    for l in patch.splitlines():
        cls = "add" if l.startswith("+") and not l.startswith("+++") else "del" if l.startswith("-") and not l.startswith("---") \
            else "hunk" if l.startswith("@@") else "meta" if l.startswith(("diff ", "index ", "---", "+++")) else ""
        rows.append(f'<span class="{cls}">{e(l)}</span>')
    return "\n".join(rows)


def _evidence_line(text: str) -> str:
    for l in (text or "").splitlines():
        if "AssertionError" in l or "RED for another" in l or "refused" in l:
            return l.strip()[:220]
    return ((text or "").splitlines() or [""])[0][:220]


def freshness(d: dict) -> str:
    """How old the newest event is: a stalled run and a finished run look the same without it (de-advisor review)."""
    if not d["events"]:
        return "no events yet"
    try:
        age = datetime.fromisoformat(d["now"]) - datetime.fromisoformat(d["events"][-1]["at"])
    except (KeyError, ValueError):
        return "unknown"
    mins = int(age.total_seconds() // 60)
    return f"{mins} min ago" if mins < 120 else f"{mins // 60} h ago"


def render(d: dict) -> str:
    s, m = d["state"], d["meter"]
    issue = s.get("issue") or {}
    outcome = s.get("outcome") or {}
    live = bool(d["next"]) and not d["interrupt"] and not outcome
    good = outcome.get("exit") in GOOD_EXITS
    status = (outcome.get("exit") if good else f"STOPPED · {outcome.get('exit')}" if outcome else "Waiting for your approval" if d["interrupt"]
              else "Running" if live else "Finished")
    clock = s.get("fix_clock") or {}
    judges = clock.get("judges")
    clock_txt = (f"{clock['seconds']:.0f} s" if clock.get("seconds") is not None else "—")
    clock_sub = ("validated · two independent tests" if clock.get("validated") else
                 "one judge only: does not count" if judges == 1 else "not validated" if clock.get("seconds") else "clock running")
    spent, cap = m.get("spent_usd", 0) or 0, m.get("cap_usd", 0) or 0
    pct = min(100, (spent / cap * 100) if cap else 0)
    model = "Claude Opus (demo)" if s.get("demo") else "dev model"

    timeline = "".join(
        f'<li class="st {r["status"].replace(" ", "-")}"><b>{i + 1}. {e(r["label"])}</b><span class="tag">{e(r["status"])}</span>'
        f'<span class="when">{e(r["span"])}</span>' + "".join(f"<p>{e(l[:260])}</p>" for l in r["lines"][-2:]) + "</li>"
        for i, r in enumerate(step_rows(d)))

    r = s.get("repro") or {}
    attempts = [a for a in s.get("attempts", []) if a.get("step") == "reproduce"]
    att_rows = "".join(f"<tr><td>{a['n']}</td><td>{e(a['rung'])}</td><td class='o {e(a['outcome'])}'>{e(a['outcome'])}</td>"
                       f"<td><code>{e(Path(a.get('test_path') or '').name)}</code></td><td>{e(_evidence_line(a.get('evidence')))}</td></tr>"
                       for a in attempts)
    confirm = {True: f"confirmed on a recorded stream ({e(r.get('confirmed_by'))})", False: "NOT confirmed on a recorded stream",
               None: "not checked on a recorded stream"}.get(r.get("confirmed"), "")

    c, f = s.get("cause") or {}, s.get("fix") or {}
    ho = f.get("holdout") or {}
    fix_rows = "".join(f"<tr><td>{a['n']}</td><td class='o {'GREEN' if a['ok'] else 'RED'}'>{'validated' if a['ok'] else 'not validated'}</td>"
                       f"<td>{e(', '.join(Path(x).name for x in a.get('changed', [])))}</td>"
                       f"<td>{e(', '.join(f'{k}: {v}' for k, v in (a.get('suites') or {}).items()))}</td>"
                       f"<td>{e(_evidence_line(a.get('evidence')))}</td></tr>" for a in f.get("attempts", []))

    g = s.get("guard") or {}
    of = g.get("on_fixed") or {}
    sib = "".join(f"<li><code>{e(x)}</code></li>" for x in g.get("siblings", []))
    cases = ("".join(f"<li class='ok'>{e(x)}</li>" for x in of.get("passed", []))
             + "".join(f"<li class='open'>{e(x)}</li>" for x in of.get("failed", []))
             + "".join(f"<li class='broken'>{e(x)}</li>" for x in of.get("broken", [])))

    b = s.get("backtest") or {}
    det = b.get("detail") or {}
    fa = det.get("false_alarms") or {}
    groups = "".join(f"<tr><td><code>{e(x['commit'])}</code></td><td>{e(x['date'])}</td><td>{x['covers']}</td>"
                     f"<td class='o {e(x['state']).replace(' ', '-')}'>{e(x['state'])}</td><td>{e(x.get('judge'))}</td>"
                     f"<td>{e(x.get('guard'))}</td><td>{e(x['title'][:70])}</td></tr>" for x in det.get("groups", []))
    caught = {True: "Yes: the guard fails where the bug was written", False: "No", None: "Unevaluable"}.get(det.get("would_have_caught"), "Not run")

    calls = "".join(f"<tr><td>{e(x.get('at', '')[11:19])}</td><td>{e(x.get('step'))}</td><td>{e(x.get('model'))}</td>"
                    f"<td class='n'>{x.get('input_tokens') or 0:,}</td><td class='n'>{x.get('output_tokens') or 0:,}</td>"
                    f"<td class='n'>${(x.get('actual_micro') or 0) / 1e6:.4f}</td><td>{e(x.get('status'))}</td></tr>" for x in d["calls"])
    evs = "".join(f"<tr><td>{e(x['at'][11:19])}</td><td>{e(x.get('step'))}</td><td>{e(x['kind'])}</td><td>{e(_event_text(x))}</td></tr>"
                  for x in d["events"])

    intr = d["interrupt"]
    approve = f"cd ~/Projects/DebugAssist && uv run debug-assist approve {d['run_id']}"
    pr_check = ("fingerprint matches the text below" if d["pr_matches"] else
                "THE FILE ON DISK DIFFERS FROM THE TEXT THAT WAS FINGERPRINTED" if d["pr_matches"] is False else "")
    md = json.dumps({"story": (s.get("second_story") or {}).get("text", ""), "pr": d["pr_text"]}).replace("</", "<\\/")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{'<meta http-equiv="refresh" content="3">' if live else ''}
<title>Debug Assist Run</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.1.6/purify.min.js"></script>
<style>
:root {{ --bg:#f6f7f9; --surface:#fff; --fg:#1f242c; --muted:#5d6675; --line:#dfe3e9; --accent:#2b5fae; --ok:#2f7a4f; --bad:#b3362b;
  --warn:#a36400; --okbg:#e3f2e8; --badbg:#fbe7e4; --warnbg:#fdf0d5; --code:#f1f3f6;
  --sans:"IBM Plex Sans",system-ui,sans-serif; --mono:"IBM Plex Mono",ui-monospace,monospace }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --bg:#0f1216; --surface:#171b21; --fg:#e7eaef; --muted:#9aa3b2;
  --line:#2a3039; --accent:#7ea6ea; --ok:#5fbf86; --bad:#ef7b6f; --warn:#e2a33b; --okbg:#1a2e22; --badbg:#3a1d1a; --warnbg:#3a2c12; --code:#1e232b; color-scheme:dark }} }}
:root[data-theme="dark"] {{ --bg:#0f1216; --surface:#171b21; --fg:#e7eaef; --muted:#9aa3b2; --line:#2a3039; --accent:#7ea6ea; --ok:#5fbf86;
  --bad:#ef7b6f; --warn:#e2a33b; --okbg:#1a2e22; --badbg:#3a1d1a; --warnbg:#3a2c12; --code:#1e232b; color-scheme:dark }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.55 var(--sans); margin:0; padding:24px 16px 48px }}
main {{ max-width:1180px; margin:0 auto; display:grid; gap:18px }}
h1 {{ font-size:24px; margin:0; font-weight:600; text-wrap:balance }} h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:0 0 10px; font-weight:500 }}
.sub {{ color:var(--muted); margin:4px 0 0 }} a {{ color:var(--accent) }}
.badge {{ display:inline-block; padding:3px 10px; border-radius:999px; font-size:13px; font-weight:500; background:var(--warnbg); color:var(--warn) }}
.badge.stopped {{ background:var(--badbg); color:var(--bad) }} .badge.running {{ background:var(--okbg); color:var(--ok) }}
.strip {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:12px }}
.stat,.card {{ background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:14px 16px; min-width:0 }}
.stat b {{ display:block; font:500 22px/1.2 var(--mono); font-variant-numeric:tabular-nums }} .stat span {{ color:var(--muted); font-size:13px }}
.bar {{ height:6px; background:var(--line); border-radius:3px; margin-top:8px; overflow:hidden }} .bar i {{ display:block; height:100%; background:var(--accent) }}
ol.tl {{ list-style:none; margin:0; padding:0; display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:10px }}
.st {{ border:1px solid var(--line); border-left:4px solid var(--line); border-radius:8px; padding:10px 12px; background:var(--surface); min-width:0 }}
.st.done {{ border-left-color:var(--ok) }} .st.stopped {{ border-left-color:var(--bad) }} .st.waiting-for-you,.st.next {{ border-left-color:var(--warn) }}
.st.not-reached {{ opacity:.55 }} .st b {{ display:block }} .st p {{ margin:6px 0 0; font-size:13px; color:var(--muted); overflow-wrap:anywhere }}
.tag {{ font-size:12px; color:var(--muted); margin-right:8px }} .when {{ font:12px var(--mono); color:var(--muted) }}
.grid2 {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(340px,1fr)); gap:14px }}
table {{ width:100%; border-collapse:collapse; font-size:13.5px }} td,th {{ padding:6px 8px; border-top:1px solid var(--line); text-align:left; vertical-align:top; overflow-wrap:anywhere }}
th {{ color:var(--muted); font-weight:500; border-top:0 }} td.n {{ font-family:var(--mono); text-align:right; white-space:nowrap }}
.scroll {{ overflow-x:auto }} code {{ font:12.5px var(--mono); background:var(--code); padding:1px 4px; border-radius:4px }}
.o {{ font-weight:600; white-space:nowrap }} .o.RED,.o.FALSE-ALARM {{ color:var(--bad) }} .o.GREEN,.o.CAUGHT,.o.QUIET {{ color:var(--ok) }} .o.ERROR,.o.UNEVALUABLE,.o.MISSED {{ color:var(--warn) }}
pre.diff {{ font:12.5px/1.5 var(--mono); background:var(--code); padding:12px; border-radius:8px; overflow-x:auto; margin:8px 0 0 }}
pre.diff span {{ display:block; white-space:pre }} .add {{ background:var(--okbg) }} .del {{ background:var(--badbg) }} .hunk {{ color:var(--accent) }} .meta {{ color:var(--muted) }}
ul.cases {{ margin:6px 0 0; padding-left:18px; font-size:13.5px }} li.ok::marker {{ content:"✓  "; color:var(--ok) }} li.open::marker {{ content:"✗  "; color:var(--bad) }} li.broken::marker {{ content:"?  "; color:var(--warn) }}
.md {{ font-size:14.5px }} .md table {{ margin:8px 0 }} .md h2 {{ font-size:15px; text-transform:none; letter-spacing:0; color:var(--fg); margin:14px 0 6px }}
.md pre {{ background:var(--code); padding:10px; border-radius:8px; overflow-x:auto; font-size:12.5px }} details > summary {{ cursor:pointer; color:var(--accent) }}
.approve {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin:8px 0 }} .approve code {{ padding:6px 8px }}
button {{ font:inherit; font-size:13px; padding:5px 12px; border:1px solid var(--line); border-radius:6px; background:var(--surface); color:var(--fg); cursor:pointer }}
.note {{ color:var(--muted); font-size:13px }} .warnbox {{ background:var(--badbg); color:var(--bad); padding:6px 10px; border-radius:6px }}
</style></head><body><main>
<header>
  <h1>{e(issue.get('repo', ''))}#{e(issue.get('number', ''))}: {e(issue.get('title', ''))}</h1>
  <p class="sub"><span class="badge {'running' if (live or good) else 'stopped' if outcome else ''}">{e(status)}</span>
   · run <code>{e(d['run_id'])}</code> · {e(model)} · <a href="{e(s.get('issue_url', ''))}">issue</a> · built {e(d['built'])}{' · refreshing' if live else ''}</p>
  {f'<p class="sub"><b>Focus:</b> {e((s.get("focus") or "")[:400])}</p>' if s.get('focus') else ''}
  {f'<p class="{"note" if good else "warnbox"}">{e(outcome.get("why"))}</p>' if outcome else ''}
</header>
<section class="strip">
  <div class="stat"><b>{e(clock_txt)}</b><span>⏱ time to validated fix · {e(clock_sub)} · from the run's checkpoint (pickup → second judge green)</span></div>
  <div class="stat"><b>${spent:.2f}</b><span>spent of ${cap:.2f} cap</span><div class="bar"><i style="width:{pct:.0f}%"></i></div></div>
  <div class="stat"><b>{e(m.get('sandbox_used_s', 0))} s</b><span>sandbox time of {e(m.get('sandbox_cap_s', '?'))} s</span></div>
  <div class="stat"><b>{e(freshness(d))}</b><span>newest event · {len(d['events'])} events in this run's log</span></div>
</section>
<section><h2>The nine steps</h2><ol class="tl">{timeline}</ol></section>
<section class="grid2">
  <div class="card"><h2>Reproduce · the ladder</h2>
    <p class="note">{e(r.get('status', ''))} at rung <b>{e(r.get('rung', ''))}</b> · {confirm} · {e(r.get('attempts_used', ''))} attempt(s)</p>
    <div class="scroll"><table><tr><th>#</th><th>rung</th><th>result</th><th>test</th><th>what decided it</th></tr>{att_rows}</table></div>
    <p class="note">Judge for the fix: <code>{e(Path(r.get('oracle_test') or '').name)}</code></p></div>
  <div class="card"><h2>Cause and fix</h2>
    <p><code>{e(c.get('file', ''))}</code> lines {e('-'.join(map(str, c.get('lines', []))))}</p><p class="note">{e(c.get('why', ''))}</p>
    <div class="scroll"><table><tr><th>#</th><th>result</th><th>changed</th><th>suites</th><th>evidence</th></tr>{fix_rows}</table></div>
    <p class="note">Second test, written without seeing the fix: <b>{e(ho.get('status', 'not run'))}</b> <code>{e(Path(ho.get('test') or '').name)}</code></p>
    {f'<details><summary>Patch</summary><pre class="diff">{_diff(d["patch"])}</pre></details>' if d['patch'] else ''}</div>
</section>
<section class="card"><h2>Why it shipped · conditions, not people</h2><div class="md" id="story"></div>
  <p class="note">Named condition (frozen before the guard): {e((s.get('condition') or {}).get('text', ''))}</p></section>
<section class="grid2">
  <div class="card"><h2>Lasting guard</h2><p>{e(g.get('covers', ''))}</p>
    <p class="note">Laya: {e(g.get('a1_class', ''))} (p={e(g.get('a1_p', ''))}) · on the fixed code: ✓ closed {len(of.get('passed', []))} · ✗ still open {len(of.get('failed', []))} · ? broken {len(of.get('broken', []))}</p>
    <details><summary>Cases on the fixed code</summary><ul class="cases">{cases}</ul></details>
    <p class="note" style="margin-top:10px">Same code in {len(g.get('siblings', []))} other file(s), not fixed here:</p><ul class="cases">{sib}</ul></div>
  <div class="card"><h2>Back-test · 🎯 would have caught</h2>
    <p><b>🎯 NOT SCORED: no past sibling (m = 0)</b>. Self-check at the anchor <code>{e((det.get('anchor') or {}).get('sha', ''))}</code>: {e(caught)} (in-sample: the guard was written from this bug)</p>
    <p class="note">On the {e(fa.get('window', '?'))} commits before: false alarms {e(fa.get('fired', '?'))} · quiet {e(fa.get('quiet', '?'))} · bug already there {e(fa.get('bug_already_there', '?'))} · unevaluable {e(fa.get('unevaluable', '?'))}</p>
    <div class="scroll"><table><tr><th>commit</th><th>date</th><th>covers</th><th>result</th><th>judge</th><th>guard</th><th>title</th></tr>{groups}</table></div></div>
</section>
<section class="card"><h2>The PR draft you approve</h2>
  {f'<p class="note">sha256 <code>{e(str(intr.get("sha256", ""))[:12])}</code> · {e(pr_check)}</p>' if intr else ''}
  {f'<div class="approve"><code id="cmd">{e(approve)}</code><button onclick="navigator.clipboard.writeText(document.getElementById(&quot;cmd&quot;).textContent)">Copy approve command</button></div><p class="note">Approving writes publish.sh, which only prints a command; nothing is posted.</p>' if intr else ''}
  <div class="md" id="pr"></div></section>
<section class="grid2">
  <div class="card"><h2>Spend, call by call</h2><div class="scroll"><table><tr><th>at</th><th>step</th><th>model</th><th>in</th><th>out</th><th>cost</th><th>status</th></tr>{calls}</table></div></div>
  <div class="card"><h2>Event log</h2><details><summary>{len(d['events'])} events</summary><div class="scroll"><table>{evs}</table></div></details>
    {f'<p class="note" style="margin-top:10px">Trials on this run (scripts that re-ran one step; never counted in the north stars):</p><ul class="cases">' + "".join(f"<li>{e(k)}: {v['n']} events ({e(', '.join(v['kinds']))})</li>" for k, v in sorted(d.get('trials', {}).items())) + '</ul>' if d.get('trials') else ''}</div>
</section>
<p class="note">Read-only. Built from this run's MongoDB state, event log and spend meter by <code>debug-assist view</code>.</p>
</main>
<script>
const MD = {md};
const show = (id, text) => {{
  const el = document.getElementById(id);
  if (!text) {{ el.textContent = "Not written yet."; return; }}
  if (window.marked && window.DOMPurify) el.innerHTML = DOMPurify.sanitize(marked.parse(text));
  else {{ const pre = document.createElement("pre"); pre.textContent = text; el.appendChild(pre); }}
}};
show("story", MD.story); show("pr", MD.pr);
</script></body></html>"""


def _event_text(x: dict) -> str:
    k = x["kind"]
    if k == "model_call":
        return f"{x.get('model')} in={x.get('input_tokens')} out={x.get('output_tokens')} ${x.get('cost_usd', x.get('charged_usd', 0)) or 0:.4f}"
    if k == "sandbox":
        return f"exit {x.get('exit')} · {x.get('seconds')} s · network {'on' if x.get('network') else 'off'}"
    if k == "laya":
        return ", ".join(f"{q}={a.get('choice', a.get('noul'))}" for q, a in (x.get("answers") or {}).items())
    if k == "attempt":
        return f"{x.get('rung')} #{x.get('n')} {x.get('outcome')}"
    if k == "step":
        return f"step finished in {x.get('seconds')} s ({x.get('ended')})"
    if k == "fix_attempt":
        return f"#{x.get('n')} {'validated' if x.get('ok') else 'not validated'}"
    return json.dumps({a: b for a, b in x.items() if a not in ("run_id", "step", "kind", "at")}, default=str)[:160]


def is_live(d: dict) -> bool:
    return bool(d["next"]) and not d["interrupt"] and not (d["state"].get("outcome"))


def write(run_id: str, out_dir: Path, watch: bool = False, every_s: float = 3.0) -> Path:
    """Write runs/<id>/view.html; with watch, keep rebuilding until the run pauses or stops."""
    import time
    path = Path(out_dir) / "view.html"
    while True:
        d = gather(run_id)
        path.write_text(render(d))
        if not (watch and is_live(d)):
            return path
        time.sleep(every_s)
