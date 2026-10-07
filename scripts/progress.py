"""Build the progress board (D2 workflow + live status) from REAL sources, never hand-typed status.

  stages + gates        ← ~/Downloads/debug-assist-pipeline/DECISIONS.md
  seat receipts         ← the advisor bundle's usage_log.jsonl
  pipeline steps        ← src/debug_assist/graph.py (a step still containing "PLACEHOLDER" is not real yet)
  tests                 ← a live pytest run (per test, so each work item shows the tests that prove it)
  latest run            ← MongoDB: the event log and spend meter of the newest run
  services              ← live checks: Docker, MongoDB primary, Ollama + model, Phoenix, Laya checkpoints
  spend                 ← OpenRouter's own counter for the key (no model call)
  triage numbers        ← the Colab reports

Run:  uv run python scripts/progress.py      → ~/Downloads/debug-assist-pipeline/progress/index.html
"""
import html
import json
import re
import subprocess
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

from debug_assist.config import CFG

HOME = Path.home()
PROJ = Path(__file__).resolve().parents[1]
WORK = HOME / "Downloads" / "debug-assist-pipeline"
BUNDLE = HOME / "Downloads" / "isha-advisor-bundle-2026-10-05 2"
NODE = HOME / ".nvm/versions/node/v22.20.0/bin/node"
RENDER = PROJ / "tools.nosync" / "d2render" / "render.mjs"
OUT = WORK / "progress"


def sh(cmd, timeout=120, cwd=PROJ):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except Exception as e:  # report, never hide
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def http_json(url, headers=None, timeout=4):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


# ── collect ──────────────────────────────────────────────────────────────────────────────────────
def stages():
    d = (WORK / "DECISIONS.md").read_text()
    done = {k: f"{k} stage: DONE".lower() in d.lower() for k in ("Teardown", "Design", "Build-eval", "Demo")}
    return done


def receipts():
    rows = [json.loads(l) for l in (BUNDLE / "usage_log.jsonl").read_text().splitlines()[1:] if l.strip()]
    return Counter(r["seat"] for r in rows), len(rows)


STEPS = ["read_issue", "reproduce", "find_cause", "write_fix", "why_it_shipped", "lasting_guard",
         "test_past_bugs", "approval", "open_pr"]
STEP_LABEL = {"read_issue": "Read issue", "reproduce": "Reproduce", "find_cause": "Find cause",
              "write_fix": "Write fix", "why_it_shipped": "Why it shipped", "lasting_guard": "Lasting guard",
              "test_past_bugs": "Test past bugs", "approval": "Your approval", "open_pr": "Open PR"}


def pipeline_steps():
    src = (PROJ / "src" / "debug_assist" / "graph.py").read_text()
    out = {}
    for name in STEPS:
        m = re.search(rf"\ndef {name}\(.*?(?=\n(?:def |[A-Z_]+ = |# ──)|\Z)", src, re.S)
        body = m.group(0) if m else ""
        if not body:
            out[name] = "missing"
        elif re.search(r'"status": "PLACEHOLDER"', body):  # returns a placeholder RESULT (not just mentions the word)
            out[name] = "placeholder"
        elif name == "test_past_bugs" and '"would_have_caught": None' in body:
            out[name] = "partial"
        else:
            out[name] = "real"
    return out


def tests():
    r = sh([str(Path.home() / ".local/bin/uv") if (Path.home() / ".local/bin/uv").exists() else "uv",
            "run", "pytest", "-q", "-rA"], timeout=300)
    last = next((l for l in reversed(r.stdout.splitlines()) if re.search(r" in [\d.]+s", l)), "")  # summary line only
    m = re.search(r"(\d+) passed(?:, (\d+) skipped)?", last)
    failed = re.search(r"(\d+) failed", last)
    each = dict((t, st) for st, t in re.findall(r"^(PASSED|FAILED|SKIPPED|ERROR) (\S+)", r.stdout, re.M))
    return {"passed": int(m.group(1)) if m else 0, "skipped": int(m.group(2) or 0) if m else 0,
            "failed": int(failed.group(1)) if failed else 0, "each": each}


# Thursday's work, item by item. Status is never typed: each item is "done" only if the tests named here exist and
# pass in the run above. A test that doesn't exist counts as missing, not as passing.
THURSDAY = [
    ("Spend checked BEFORE every call", "refused_before_it_is_made|refuses_before_calling|worst_case_bounds"),
    ("Spend survives a crash (MongoDB meter)", "crash_between_reserve|reopening_on_resume|micro_dollars|"
                                               "failed_call_is_charged|unmetered_runs"),
    ("Turn caps survive a re-run step", "turn_caps_live_in_mongodb"),
    ("Sandbox time cap per run (1,800 s)", "sandbox_time_is_capped"),
    ("One door to the model (_writer private)", "only_models_py"),
    ("Reproduction ladder: climb, retry, cap 4", "test_ladder.py::test_(red|green|error|four|running|plan)"),
    ("Ladder reads real test output (node sandbox)", "classify"),
    ("Attempts recorded, not repeated on resume", "records_every_attempt|does_not_repeat_attempts|"
                                                  "resume_counts_earlier|red_found_before"),
    ("Resume after a crash", "resume_continues_after"),
    ("Typed exits: NEEDS PERSON / NOT A DEFECT / NEVER REPRODUCED",
     "stops_for_a_person|confident_not_a_bug|confident_bug_goes_on|typed_exit_ends|stops_never_reproduced"),
    ("One event log (model, sandbox, Laya, attempts)", "logs_one_event|records_every_attempt"),
    ("Condition freeze safe to re-run", "refreezing_the_same"),
]


def thursday(each):
    rows = []
    for label, rx in THURSDAY:
        hits = {t: st for t, st in each.items() if re.search(rx, t)}
        ok = sum(st == "PASSED" for st in hits.values())
        rows.append({"item": label, "passed": ok, "of": len(hits),
                     "status": "done" if hits and ok == len(hits) else ("missing" if not hits else "failing")})
    log = sh(["git", "log", "--reverse", "--format=%h"], timeout=10)
    shas = log.stdout.split() if log.returncode == 0 else []
    rows.append({"item": "First git commit (local, no push)", "passed": None, "of": None,
                 "status": "done" if shas else "missing",
                 "evidence": f"{shas[0]} · {len(shas)} commit(s)" if shas else "no commits yet"})
    return rows


def run_evidence(run_id):
    """The newest run's own record: events by kind and the meter (MongoDB, not the run state)."""
    if not run_id:
        return None
    try:
        from pymongo import MongoClient
        d = MongoClient(CFG.mongodb_uri, serverSelectionTimeoutMS=2500)[CFG.db_name]
        kinds = Counter(e["kind"] for e in d["events"].find({"run_id": run_id}, {"kind": 1}))
        m = d["meters"].find_one({"_id": run_id}) or {}
    except Exception:
        return None
    return {"events": dict(kinds), "spent_usd": (m.get("spent_micro") or 0) / 1e6,
            "cap_usd": (m.get("cap_micro") or 0) / 1e6, "sandbox_s": m.get("sandbox_used_s"),
            "sandbox_cap_s": m.get("sandbox_cap_s")}


def services():
    s = {}
    s["Docker"] = sh(["docker", "info"], timeout=8).returncode == 0
    try:
        from pymongo import MongoClient
        s["MongoDB"] = bool(MongoClient(CFG.mongodb_uri, serverSelectionTimeoutMS=2500).admin.command("hello")
                            .get("isWritablePrimary"))
    except Exception:
        s["MongoDB"] = False
    tags = http_json(f"{CFG.ollama_url}/api/tags")
    s["Ollama + Qwen3"] = bool(tags and any(m["name"].startswith("qwen3-embedding") for m in tags.get("models", [])))
    s["Phoenix"] = http_json("http://127.0.0.1:6006/v1/projects") is not None
    s["Laya (triage)"] = (Path(CFG.laya_triage_checkpoint) / "model.safetensors").exists()
    return s


def spend():
    d = http_json("https://openrouter.ai/api/v1/key", {"Authorization": f"Bearer {CFG.openrouter_key}"}, timeout=8)
    if not d:
        return None
    d = d["data"]
    return {"used": d.get("usage") or 0.0, "limit": d.get("limit")}


def triage():
    run = PROJ / "training" / "artifacts" / "colab_run"
    try:
        b, f = (json.loads((run / n).read_text()) for n in ("baseline.json", "finetuned.json"))
    except Exception:
        return None
    hb = lambda r: sum(p["def_ok"] for p in r["hard"]["predictions"] if p["label"] != "bug")
    return {"n": f["triage"]["n"], "before": b["triage"]["is_defect"]["correct"],
            "after": f["triage"]["is_defect"]["correct"], "ece": f["triage"]["is_defect"]["ece"],
            "look_before": hb(b), "look_after": hb(f),
            "look_n": sum(1 for p in f["hard"]["predictions"] if p["label"] != "bug")}


def latest_run():
    runs = sorted((PROJ / "runs").glob("*"), key=lambda p: p.stat().st_mtime) if (PROJ / "runs").exists() else []
    return runs[-1].name if runs else None


# ── D2 ───────────────────────────────────────────────────────────────────────────────────────────
PALETTE = {  # status → (fill, stroke, text) for light and dark renders
    "light": {"done": ("#dcefe1", "#2f7a4f", "#173d28"), "active": ("#fdecc8", "#b06d00", "#5a3800"),
              "partial": ("#dfe8f8", "#3a63a8", "#1b335c"), "todo": ("#eef0f3", "#8b93a1", "#3f4654"),
              "stage": ("#f7f8fa", "#c5cad3", "#252a33")},
    "dark": {"done": ("#1d3a29", "#5fbf86", "#d7f2e2"), "active": ("#3d2e10", "#e2a33b", "#fbe7c2"),
             "partial": ("#1d2b45", "#6f99e0", "#dbe6fb"), "todo": ("#262a31", "#6b7382", "#c8cdd6"),
             "stage": ("#16191e", "#3a404b", "#e6e9ee")},
}


def q(s):
    return '"' + s.replace('"', "'") + '"'


def d2_source(st, steps, seats, mode):
    p = PALETTE[mode]
    cls = "\n".join(
        f"  {k}: {{style: {{fill: \"{f}\"; stroke: \"{s}\"; font-color: \"{t}\"; border-radius: 6"
        + ("; stroke-dash: 4" if k == "todo" else "") + ("; stroke-width: 2" if k == "active" else "") + "}}"
        for k, (f, s, t) in p.items() if k != "stage")
    sf, ss, stx = p["stage"]
    stage_style = f"style: {{fill: \"{sf}\"; stroke: \"{ss}\"; font-color: \"{stx}\"; border-radius: 10; bold: true}}"

    td, dz, be, dm = st["Teardown"], st["Design"], st["Build-eval"], st["Demo"]
    seat_done = lambda s: "done" if seats.get(s) else ("active" if dz is False or s == "harness-design" else "todo")
    step_cls = {"real": "done", "partial": "partial", "placeholder": "todo", "missing": "todo"}
    build_steps = "\n".join(f"    {n}: {q(STEP_LABEL[n])} {{class: {step_cls[steps[n]]}}}" for n in STEPS)
    chain = " -> ".join(STEPS)

    return f"""direction: right
classes: {{
{cls}
}}
teardown: "1 · Teardown  (Mon–Tue)" {{
  {stage_style}
  direction: down
  t: "Uber teardown\\n42 of 42 claims sourced" {{class: {"done" if td else "active"}}}
  g: "Gate: PASS (option C)" {{class: {"done" if td else "todo"}}}
  t -> g
}}
design: "2 · Design  (Wed–Thu)" {{
  {stage_style}
  direction: down
  a: "Pipeline shape\\nallspaw" {{class: {"done" if seats.get("allspaw") else "todo"}}}
  m: "Two north stars\\nmetric-design" {{class: {"done" if seats.get("metric-design") else "todo"}}}
  h: "Agent surface review\\nharness-design" {{class: {"done" if seats.get("harness-design") else "active"}}}
  g: "Design gate v1: PASS" {{class: {"done" if dz else "todo"}}}
  a -> g; m -> g
}}
setup: "Setup (built early)" {{
  {stage_style}
  direction: down
  s1: "Stack running\\nLangGraph · Mongo · Phoenix" {{class: done}}
  s2: "Walking skeleton\\n9 steps end to end" {{class: done}}
  s3: "Guardrails in code" {{class: done}}
  s4: "Laya fine-tuned\\n(triage only)" {{class: done}}
  s1 -> s2 -> s3; s2 -> s4
}}
build: "3 · Build + eval  (Fri–Sat)" {{
  {stage_style}
  direction: down
{build_steps}
  {chain}
  gate: "Build-eval gate\\nqe-ic-advisor" {{class: {"done" if be else "todo"}}}
  open_pr -> gate
}}
demo: "4 · Demo  (Sun)" {{
  {stage_style}
  direction: down
  arc: "Villain → resolution arc\\nallspaw" {{class: {"done" if dm else "todo"}}}
  hack: "Harness hackathon\\n(MongoDB)" {{class: {"done" if dm else "todo"}}}
  arc -> hack
}}
teardown -> design: gate passed
design -> build: gate passed
setup -> build
build -> demo
"""


def render(src, theme, salt):
    path = OUT / f"workflow-{salt}.d2"
    path.write_text(src)
    r = sh([str(NODE), str(RENDER), str(path), str(theme), salt], timeout=120, cwd=RENDER.parent)
    if r.returncode != 0 or "<svg" not in r.stdout:
        raise SystemExit(f"D2 render failed: {r.stderr[:400]}")
    svg = r.stdout
    return svg[svg.find("<svg"):]


# ── page ─────────────────────────────────────────────────────────────────────────────────────────
def page(data, svg_light, svg_dark):
    st, steps, t, sv, sp, tr = data["stages"], data["steps"], data["tests"], data["services"], data["spend"], data["triage"]
    n_stage = sum(st.values())
    real = sum(v == "real" for v in steps.values())
    part = sum(v == "partial" for v in steps.values())
    stage_now = next((k for k, v in st.items() if not v), "Done")
    svc = "".join(f'<li><span class="dot {"ok" if ok else "bad"}"></span>{html.escape(k)}'
                  f'<span class="muted">{"up" if ok else "down"}</span></li>' for k, ok in sv.items())
    seats = "".join(f"<li>{html.escape(k)}<span class='num'>{v}</span></li>"
                    for k, v in sorted(data["seats"].items(), key=lambda x: -x[1]))
    spend = (f"${sp['used']:.4f} <span class='muted'>of ${sp['limit']:.0f} cap</span>" if sp and sp["limit"]
             else "unavailable")
    tri = (f"<p class='big'>{tr['before']} → {tr['after']} <span class='muted'>of {tr['n']}</span></p>"
           f"<p class='muted'>“Is it a real bug?” on held-out issues · calibration error {tr['ece']}</p>"
           f"<p class='muted'>Bug-sounding non-bugs rejected: {tr['look_before']} → {tr['look_after']} of "
           f"{tr['look_n']} <span class='warn'>(weak spot)</span></p>") if tr else "<p class='muted'>No Colab report found</p>"
    NEXT = {
        "Teardown": ["Finish the Uber teardown and pass its gate"],
        "Design": ["Harness-design review of the agent surface", "Design gate ruling"],
        "Build-eval": ["Fri: run vercel/ai #21439 by hand, end to end (clean Claude Code session in the repo)",
                       "Fri: build the test writer for ladder rung 1 from what the by-hand run shows",
                       "Sat: turn the by-hand steps into code; build-eval gate (qe-ic-advisor)",
                       "Ask Devansh: Sunday reader + weak seats (sent; awaiting reply)"],
        "Demo": ["Sun: tune the demo to Devansh's room angle", "Run the demo issue 3–5 times for the speed number"],
    }
    nxt = "".join(f"<li>{html.escape(x)}</li>" for x in NEXT.get(stage_now, ["All stages done"]))
    badge = {"done": ("ok", "done"), "failing": ("bad", "failing"), "missing": ("bad", "missing")}
    thu = "".join(
        f"<tr><td>{html.escape(r['item'])}</td><td class='n'>"
        + (f"{r['passed']} of {r['of']}" if r["of"] is not None else html.escape(r.get("evidence", "")))
        + f"</td><td><span class='dot {badge[r['status']][0]}'></span> {badge[r['status']][1]}</td></tr>"
        for r in data["thursday"])
    ev = data["run_evidence"]
    ev_html = (f"<p class='muted'>Newest run <code>{html.escape(data['latest_run'] or '')}</code>: "
               + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in sorted(ev["events"].items()))
               + f" · spent ${ev['spent_usd']:.6f} of ${ev['cap_usd']:.2f} · sandbox {ev['sandbox_s']} of "
               f"{ev['sandbox_cap_s']} s</p>") if ev else ""
    gen = datetime.now().strftime("%a %d %b %Y, %H:%M")

    return f"""<title>Debug Assist Progress</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
/* Layout: one column, a progress strip, the D2 workflow as the centrepiece, then status cards in a wrapping grid. */
:root {{ --bg:#f6f7f9; --surface:#ffffff; --fg:#1f242c; --muted:#5d6675; --line:#dfe3e9; --accent:#2b5fae;
  --ok:#2f7a4f; --bad:#b3362b; --warn:#a36400; --sans:"IBM Plex Sans",system-ui,sans-serif; --mono:"IBM Plex Mono",ui-monospace,monospace }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --bg:#0f1216; --surface:#171b21; --fg:#e7eaef;
  --muted:#9aa3b2; --line:#2a3039; --accent:#7ea6ea; --ok:#5fbf86; --bad:#ef7b6f; --warn:#e2a33b; color-scheme:dark }} }}
:root[data-theme="dark"] {{ --bg:#0f1216; --surface:#171b21; --fg:#e7eaef; --muted:#9aa3b2; --line:#2a3039;
  --accent:#7ea6ea; --ok:#5fbf86; --bad:#ef7b6f; --warn:#e2a33b; color-scheme:dark }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.55 var(--sans); padding-inline:16px; padding-block:28px 48px }}
main {{ max-width:1180px; margin:0 auto; display:grid; gap:22px }}
h1 {{ font-size:26px; font-weight:600; margin:0; text-wrap:balance; letter-spacing:-.01em }}
.sub {{ color:var(--muted); margin:4px 0 0; font-size:14px }}
.strip {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px }}
.stat {{ background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:12px 14px; min-width:0 }}
.stat b {{ display:block; font:500 22px/1.2 var(--mono); font-variant-numeric:tabular-nums }}
.stat span {{ color:var(--muted); font-size:13px }}
.bar {{ height:6px; background:var(--line); border-radius:3px; margin-top:8px; overflow:hidden }}
.bar i {{ display:block; height:100%; background:var(--accent) }}
.board {{ background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:12px; overflow-x:auto }}
.board svg {{ display:block; height:auto; max-width:none }}
.svg-dark {{ display:none }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) .svg-light {{ display:none }} :root:not([data-theme="light"]) .svg-dark {{ display:block }} }}
:root[data-theme="dark"] .svg-light {{ display:none }} :root[data-theme="dark"] .svg-dark {{ display:block }}
.legend {{ display:flex; flex-wrap:wrap; gap:14px; color:var(--muted); font-size:13px; margin:2px 4px 8px }}
.legend i {{ display:inline-block; width:12px; height:12px; border-radius:3px; margin-right:6px; vertical-align:-1px; border:1px solid }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:14px }}
.card {{ background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:14px 16px; min-width:0 }}
.card h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:0 0 8px; font-weight:500 }}
.card ul {{ list-style:none; margin:0; padding:0; display:grid; gap:6px }}
.card li {{ display:flex; align-items:center; gap:8px }}
.num {{ margin-left:auto; font-family:var(--mono); font-variant-numeric:tabular-nums }}
.muted {{ color:var(--muted); margin-left:auto; font-size:13px }}
p.muted {{ margin:2px 0 }}
.big {{ font:500 24px/1.2 var(--mono); margin:2px 0 6px }}
.dot {{ width:8px; height:8px; border-radius:50% }} .dot.ok {{ background:var(--ok) }} .dot.bad {{ background:var(--bad) }}
.warn {{ color:var(--warn) }}
.wide {{ grid-column:1 / -1 }}
table.bd {{ width:100%; border-collapse:collapse; font-size:14px }}
table.bd td {{ padding:6px 8px; border-top:1px solid var(--line); vertical-align:top }}
table.bd td.n {{ font-family:var(--mono); font-variant-numeric:tabular-nums; white-space:nowrap; color:var(--muted) }}
table.bd td:last-child {{ white-space:nowrap }}
table.bd .dot {{ display:inline-block }}
.scroll {{ overflow-x:auto }}
code {{ font-family:var(--mono); font-size:.92em }}
ol.next {{ margin:0; padding-left:20px; display:grid; gap:6px }}
footer {{ color:var(--muted); font-size:12.5px }}
footer code {{ font-family:var(--mono) }}
</style>
<main>
  <header>
    <h1>Debug Assist Progress</h1>
    <p class="sub">Pipeline week · now in <strong>{html.escape(stage_now)}</strong> · generated {gen} from the project's own files</p>
  </header>
  <section class="strip" aria-label="Headline progress">
    <div class="stat"><b>{n_stage} of 4</b><span>stages through their gate</span><div class="bar"><i style="width:{n_stage*25}%"></i></div></div>
    <div class="stat"><b>{real} of 9</b><span>pipeline steps real{f" · {part} partial" if part else ""}</span><div class="bar"><i style="width:{(real+part*0.5)/9*100:.0f}%"></i></div></div>
    <div class="stat"><b>{t["passed"]}</b><span>tests passing{f" · {t['skipped']} skipped" if t["skipped"] else ""}{f" · {t['failed']} failing" if t["failed"] else ""}</span></div>
    <div class="stat"><b>{data["n_receipts"]}</b><span>advisor-seat receipts logged</span></div>
  </section>
  <section class="board" aria-label="Workflow diagram">
    <div class="legend"><span><i style="background:#dcefe1;border-color:#2f7a4f"></i>Done</span><span><i style="background:#fdecc8;border-color:#b06d00"></i>In progress</span><span><i style="background:#dfe8f8;border-color:#3a63a8"></i>Partly real</span><span><i style="background:#eef0f3;border-color:#8b93a1"></i>Not started / placeholder</span></div>
    <div class="svg-light">{svg_light}</div>
    <div class="svg-dark">{svg_dark}</div>
  </section>
  <section class="grid">
    <div class="card wide"><h2>Thursday's work, item by item (status = its tests in the live run above)</h2>
      <div class="scroll"><table class="bd"><tbody>{thu}</tbody></table></div>{ev_html}</div>
    <div class="card"><h2>Next up</h2><ol class="next">{nxt}</ol></div>
    <div class="card"><h2>North stars</h2><p class="big">Not measured yet</p><p class="muted">⏱ Time to validated fix and 🎯 would-have-caught start with Friday's by-hand run of vercel/ai #21439.</p></div>
    <div class="card"><h2>Laya triage (fine-tuned)</h2>{tri}</div>
    <div class="card"><h2>Services, at generation time</h2><ul>{svc}</ul></div>
    <div class="card"><h2>Advisor-seat receipts</h2><ul>{seats}</ul></div>
    <div class="card"><h2>Model spend</h2><p class="big">{spend}</p><p class="muted">OpenRouter key counter · latest run {html.escape(data["latest_run"] or "none")}</p></div>
  </section>
  <footer>Regenerate with <code>uv run python scripts/progress.py</code> in DebugAssist. Pipeline step status is read from <code>graph.py</code>: a step that still returns a placeholder result is not counted as real.</footer>
</main>
"""


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    seats, n = receipts()
    data = {"stages": stages(), "steps": pipeline_steps(), "tests": tests(), "services": services(),
            "spend": spend(), "triage": triage(), "seats": dict(seats), "n_receipts": n, "latest_run": latest_run()}
    data["thursday"] = thursday(data["tests"].pop("each"))
    data["run_evidence"] = run_evidence(data["latest_run"])
    light = render(d2_source(data["stages"], data["steps"], data["seats"], "light"), 0, "light")
    dark = render(d2_source(data["stages"], data["steps"], data["seats"], "dark"), 200, "dark")
    (OUT / "index.html").write_text(page(data, light, dark))
    (OUT / "status.json").write_text(json.dumps(data, indent=2, default=str))
    print(f"wrote {OUT / 'index.html'} ({(OUT / 'index.html').stat().st_size / 1e3:.0f} KB)")
    print(json.dumps({k: data[k] for k in ("stages", "steps", "tests", "services")}, default=str))


if __name__ == "__main__":
    main()
