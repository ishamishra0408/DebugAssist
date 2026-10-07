"""The run viewer: one read-only page per run, built from MongoDB (the run's checkpointed state, its event log and
its spend meter). Ruled 2026-10-07: read-only, approval stays in the terminal (the go-word is a guardrail; the page
has no button that acts, only one that copies the approve command).

  uv run debug-assist view <run-id> [--watch]     → runs/<run-id>/view.html (with --watch, rebuilt every 3 s and the
                                                     page reloads itself until the run pauses or stops)
  uv run debug-assist serve                       → http://127.0.0.1:8777/ (server.py): the same page, updated in
                                                     place every 2 s; `run` and `resume` open it themselves.
                                                     /replay/<id> plays a recorded run back from its checkpoints.

Everything that came from outside (issue text, PR descriptions in the evidence, model-written story and PR text) is
escaped, and markdown is rendered in the browser through DOMPurify, so no text in a run can run script in the page.
"""
import hashlib
import html
import json
import re
from datetime import datetime
from pathlib import Path

from . import plain

_FIELD = {"read_issue": "triage", "reproduce": "repro", "find_cause": "cause", "write_fix": "fix",
          "why_it_shipped": "second_story", "lasting_guard": "guard", "test_past_bugs": "backtest",
          "approval": "approval", "open_pr": "published"}
STEPS = [(k, label, _FIELD[k]) for k, label, _ in plain.STEPS]  # (graph node, operator's name, state field)
e = lambda x: html.escape(str(x if x is not None else ""))
GOOD_EXITS = {"READY FOR YOU TO PUBLISH"}  # a finished run, not a failure: shown green


_APP = None


def _app():
    global _APP
    if _APP is None:
        from .graph import build
        _APP = build()
    return _APP


def _t(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def pick(history: list, at: datetime):
    """The checkpoint a run was at, at time `at` (history newest first, as LangGraph returns it); None before it began."""
    return next((h for h in history if _t(h.created_at) <= at), None)


def gather(run_id: str, at: datetime | None = None) -> dict:
    """Everything the page shows, read once. Separate from render() so tests can render without MongoDB.
    With `at`, the run as it was then: its checkpoint, events, calls and spend up to that moment (replay)."""
    from types import SimpleNamespace
    from . import events, meter
    from .store import db
    app, cfg = _app(), {"configurable": {"thread_id": run_id}}
    snap = app.get_state(cfg) if at is None else pick(list(app.get_state_history(cfg)), at)
    snap = snap or SimpleNamespace(values={}, next=(), tasks=(), created_at=None, metadata={})
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
    evs, mtr = events.for_run(run_id), meter.snapshot(run_id) or {}
    from .config import CFG
    con = CFG.runs_dir / run_id / "console.log"  # runs started from the home page write their terminal output here
    console = con.read_text(errors="replace")[-6000:] if con.exists() and at is None else ""
    if at is not None:  # replay: nothing from after `at`
        evs = [x for x in evs if _t(x["at"]) <= at]
        calls = [c for c in calls if c.get("at") and _t(c["at"]) <= at]
        mtr = {**mtr, "spent_usd": sum((c.get("actual_micro") or 0) for c in calls) / 1e6,
               "sandbox_used_s": round(sum(x.get("seconds") or 0 for x in evs if x["kind"] == "sandbox"))}
    return {"run_id": run_id, "state": snap.values or {}, "next": list(snap.next or []), "interrupt": intr,
            "pr_text": pr_text, "pr_matches": pr_ok, "patch": patch, "events": evs,
            "trials": events.trials_of(run_id), "meter": mtr, "calls": calls,
            "built": datetime.now().strftime("%H:%M:%S"),
            "now": (at or datetime.now().astimezone()).isoformat(),
            "since": snap.created_at,  # the last checkpoint: the step in `next` started then
            "edited": (snap.metadata or {}).get("source") == "update",  # state written outside the pipeline
            "console": console}


def replay_plan(run_id: str, speed: float = 8.0, max_gap_s: float = 4.0) -> list[tuple[float, datetime]]:
    """Every moment of a recorded run (its checkpoints and events) on a replay clock: real gaps / speed, and no wait
    longer than max_gap_s (a run that sat at approval for 25 min replays it in 4 s)."""
    from . import events
    cfg = {"configurable": {"thread_id": run_id}}
    times = sorted({_t(h.created_at) for h in _app().get_state_history(cfg)} | {_t(x["at"]) for x in events.for_run(run_id)})
    return plan_from(times, speed, max_gap_s)


def plan_from(times: list[datetime], speed: float, max_gap_s: float) -> list[tuple[float, datetime]]:
    plan, off = [], 0.0
    for i, t in enumerate(times):
        if i:
            off += min((t - times[i - 1]).total_seconds() / speed, max_gap_s)
        plan.append((round(off, 3), t))
    return plan


def real_at(plan: list[tuple[float, datetime]], elapsed_s: float) -> datetime | None:
    """The recorded moment a replay `elapsed_s` seconds in is showing (time runs evenly inside each gap)."""
    if not plan:
        return None
    if elapsed_s <= plan[0][0]:
        return plan[0][1]
    for (o1, t1), (o2, t2) in zip(plan, plan[1:]):
        if elapsed_s < o2:
            return t1 + (t2 - t1) * ((elapsed_s - o1) / (o2 - o1))
    return plan[-1][1]


def step_rows(d: dict) -> list[dict]:
    s, out = d["state"], []
    stop = (s.get("outcome") or {})
    logs = s.get("log", [])
    # Where a run stopped: the step whose own `step` event ended with the exit; else the last step that logged
    # STOPPED; else the last step with output. Only for a stop: a run that ended well has none (a STOPPED line
    # left by an earlier, resumed attempt marked a finished run as stopped at lasting_guard; found 2026-10-07).
    stopped_at, line_at, bad = None, None, bool(stop) and stop.get("exit") not in GOOD_EXITS
    ended = {x["step"]: x.get("ended") for x in d["events"] if x.get("kind") == "step"}
    for i, (key, label, field) in enumerate(STEPS):
        lines = [l.split(":", 1)[1].strip() for l in logs if l.startswith(key + ":")]
        if bad and ended.get(key) == stop.get("exit"):
            stopped_at = i
        if bad and lines and any("STOPPED" in l for l in lines):
            line_at = i  # the last one wins: an earlier STOPPED may be from a resumed attempt
        if field in s and s.get(field) not in (None, {}):
            status = "done"
        elif key in d["next"]:
            status = "waiting for you" if d["interrupt"] else "next"
        else:
            status = "not reached"
        ev = [x for x in d["events"] if x.get("step") == key]
        span = f"{ev[0]['at'][11:19]}–{ev[-1]['at'][11:19]}" if ev else ""
        out.append({"key": key, "label": label, "status": status, "lines": lines, "span": span, "events": len(ev)})
    if bad and stopped_at is None:
        stopped_at = line_at
    if bad and stopped_at is None:
        stopped_at = max((i for i, r in enumerate(out) if r["status"] == "done"), default=None)
    if stopped_at is not None:
        out[stopped_at]["status"] = "stopped"
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
    secs = age.total_seconds()
    if secs < 90:
        return f"{max(0, secs):.0f} s ago"
    mins = int(secs // 60)
    return f"{mins} min ago" if mins < 120 else f"{mins // 60} h ago"


def _wouldve(b: dict) -> str:
    """🎯 for one run, from its own back-test state (north-star-v1.1: k of m past siblings)."""
    m = len(b.get("candidates") or [])
    if m:
        return f"🎯 NOT SCORED YET: {m} candidate sibling(s), unconfirmed"
    others = b.get("other_issues", 0 if b.get("searched_conditions") == 0 else None)
    return "🎯 NOT SCORED: no past sibling (m = 0)" + (
        "; the corpus held no other issue to search" if others == 0 else "")

# What each step is, in the pipeline card: (kind, what runs it). The model is filled in per run.
_CSS = ''':root { --bg:#f6f7f9; --surface:#fff; --fg:#1f242c; --muted:#5d6675; --line:#dfe3e9; --accent:#2b5fae; --ok:#2f7a4f; --bad:#b3362b;
  --warn:#a36400; --okbg:#e3f2e8; --badbg:#fbe7e4; --warnbg:#fdf0d5; --code:#f1f3f6;
  --sans:"IBM Plex Sans",system-ui,sans-serif; --mono:"IBM Plex Mono",ui-monospace,monospace }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#0f1216; --surface:#171b21; --fg:#e7eaef; --muted:#9aa3b2;
  --line:#2a3039; --accent:#7ea6ea; --ok:#5fbf86; --bad:#ef7b6f; --warn:#e2a33b; --okbg:#1a2e22; --badbg:#3a1d1a; --warnbg:#3a2c12; --code:#1e232b; color-scheme:dark } }
:root[data-theme="dark"] { --bg:#0f1216; --surface:#171b21; --fg:#e7eaef; --muted:#9aa3b2; --line:#2a3039; --accent:#7ea6ea; --ok:#5fbf86;
  --bad:#ef7b6f; --warn:#e2a33b; --okbg:#1a2e22; --badbg:#3a1d1a; --warnbg:#3a2c12; --code:#1e232b; color-scheme:dark }
body { background:var(--bg); color:var(--fg); font:15px/1.55 var(--sans); margin:0; padding:24px 16px 48px }
main { max-width:1180px; margin:0 auto; display:grid; grid-template-columns:minmax(0,1fr); gap:18px } main > * { min-width:0 }
h1 { font-size:24px; margin:0; font-weight:600; text-wrap:balance } h2 { font-size:13px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:0 0 10px; font-weight:500 }
.sub { color:var(--muted); margin:4px 0 0 } a { color:var(--accent) }
.badge { display:inline-block; padding:3px 10px; border-radius:999px; font-size:13px; font-weight:500; background:var(--warnbg); color:var(--warn) }
.badge.stopped { background:var(--badbg); color:var(--bad) } .badge.running { background:var(--okbg); color:var(--ok) }
.strip { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:12px }
.stat,.card { background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:14px 16px; min-width:0 }
.stat b { display:block; font:500 22px/1.2 var(--mono); font-variant-numeric:tabular-nums } .stat span { color:var(--muted); font-size:13px }
.bar { height:6px; background:var(--line); border-radius:3px; margin-top:8px; overflow:hidden } .bar i { display:block; height:100%; background:var(--accent) }
ol.tl { list-style:none; margin:0; padding:0; display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:10px }
.st { border:1px solid var(--line); border-left:4px solid var(--line); border-radius:8px; padding:10px 12px; background:var(--surface); min-width:0 }
.st.done { border-left-color:var(--ok) } .st.stopped { border-left-color:var(--bad) } .st.waiting-for-you,.st.next { border-left-color:var(--warn) }
.st.not-reached { opacity:.55 } .st b { display:block } .st p { margin:6px 0 0; font-size:13px; color:var(--muted); overflow-wrap:anywhere }
.tag { font-size:12px; color:var(--muted); margin-right:8px } .when { font:12px var(--mono); color:var(--muted) }
.grid2 { display:grid; grid-template-columns:repeat(auto-fit,minmax(340px,1fr)); gap:14px }
table { width:100%; border-collapse:collapse; font-size:13.5px } td,th { padding:6px 8px; border-top:1px solid var(--line); text-align:left; vertical-align:top; overflow-wrap:anywhere }
th { color:var(--muted); font-weight:500; border-top:0 } td.n { font-family:var(--mono); text-align:right; white-space:nowrap }
.scroll { overflow-x:auto } code { font:12.5px var(--mono); background:var(--code); padding:1px 4px; border-radius:4px }
.o { font-weight:600; white-space:nowrap } .o.RED,.o.FALSE-ALARM { color:var(--bad) } .o.GREEN,.o.CAUGHT,.o.QUIET { color:var(--ok) } .o.ERROR,.o.UNEVALUABLE,.o.MISSED { color:var(--warn) }
pre.diff { font:12.5px/1.5 var(--mono); background:var(--code); padding:12px; border-radius:8px; overflow-x:auto; margin:8px 0 0 }
pre.diff span { display:block; white-space:pre } .add { background:var(--okbg) } .del { background:var(--badbg) } .hunk { color:var(--accent) } .meta { color:var(--muted) }
ul.cases { margin:6px 0 0; padding-left:18px; font-size:13.5px } li.ok::marker { content:"✓  "; color:var(--ok) } li.open::marker { content:"✗  "; color:var(--bad) } li.broken::marker { content:"?  "; color:var(--warn) }
.md { font-size:14.5px } .md table { margin:8px 0 } .md h2 { font-size:15px; text-transform:none; letter-spacing:0; color:var(--fg); margin:14px 0 6px }
.md pre { background:var(--code); padding:10px; border-radius:8px; overflow-x:auto; font-size:12.5px } details > summary { cursor:pointer; color:var(--accent) }
.approve { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin:8px 0 } .approve code { padding:6px 8px }
button { font:inherit; font-size:13px; padding:5px 12px; border:1px solid var(--line); border-radius:6px; background:var(--surface); color:var(--fg); cursor:pointer }
.note { color:var(--muted); font-size:13px }
.pl { --pbg:#0a0c10; --pbox:#12161c; --pline:#1f252d; --pfg:#e6e9ee; --pmuted:#7b8494; --pblue:#5b84ff; --pgreen:#3ecf8e;
  --pamber:#e5a53c; --pred:#f06b60; background:var(--pbg); color:var(--pfg); border:1px solid var(--pline); border-radius:14px;
  overflow:hidden; font-family:var(--mono) }
.pl-head { display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; padding:12px 18px; border-bottom:1px solid var(--pline);
  font-size:11px; letter-spacing:.09em; text-transform:uppercase; color:var(--pmuted) }
.pl-dot { display:inline-block; width:7px; height:7px; border-radius:50%; margin-right:9px; background:var(--pmuted); vertical-align:1px }
.pl.live .pl-dot,.pl.done .pl-dot { background:var(--pgreen) } .pl.live .pl-dot { animation:plpulse 1.6s ease-in-out infinite }
.pl.waiting .pl-dot { background:var(--pamber) } .pl.stopped .pl-dot { background:var(--pred) }
.pl-flow { list-style:none; margin:0; padding:30px 18px 22px; display:grid; grid-template-columns:repeat(9,minmax(0,1fr)); gap:20px }
.node { position:relative; min-width:0; text-align:center }
.node .box { position:relative; background:var(--pbox); border:1px solid var(--pline); border-radius:10px; padding:10px 6px 9px;
  transition:border-color .3s, box-shadow .3s }
.node .box::after { content:""; position:absolute; top:7px; right:7px; width:6px; height:6px; border-radius:50% }
.node .k { font-size:9.5px; letter-spacing:.11em; text-transform:uppercase; color:var(--pmuted) }
.node .t { font:500 13px/1.25 var(--sans); margin-top:4px; color:var(--pfg); overflow-wrap:anywhere }
.node .s { font-size:10.5px; margin-top:5px; color:var(--pmuted); min-height:14px }
.node .cap { font-size:9.5px; color:var(--pmuted); margin-top:7px; opacity:.75; overflow-wrap:anywhere }
.node.done .box { border-color:#1f3a2d } .node.done .box::after { background:var(--pgreen) } .node.done .s { color:var(--pgreen) }
.node.running .box { border-color:var(--pblue); background:#0f1830; box-shadow:0 0 0 1px var(--pblue),0 0 26px rgba(91,132,255,.38) }
.node.running .k,.node.running .cap { color:var(--pblue); opacity:1 } .node.running .box::after { background:var(--pblue); animation:plpulse 1.2s infinite }
.node.waiting .box { border-color:var(--pamber); box-shadow:0 0 22px rgba(229,165,60,.25) } .node.waiting .box::after { background:var(--pamber) }
.node.waiting .s { color:var(--pamber) }
.node.stopped .box { border-color:var(--pred); box-shadow:0 0 18px rgba(240,107,96,.22) } .node.stopped .box::after { background:var(--pred) }
.node.stopped .s { color:var(--pred) } .node.pending { opacity:.42 }
.dots { display:inline-flex; gap:4px } .dots i { width:5px; height:5px; border-radius:50%; background:var(--pblue); animation:plblink 1.2s infinite }
.dots i:nth-child(2) { animation-delay:.2s } .dots i:nth-child(3) { animation-delay:.4s }
.edge { position:absolute; top:31px; left:100%; width:20px; height:4px; color:var(--pline);
  background-image:radial-gradient(circle,currentColor 1.3px,transparent 1.7px); background-size:6px 4px; background-repeat:repeat-x }
.edge.lit { color:#2c6b52 } .edge.flow { color:var(--pblue); animation:plflow .6s linear infinite }
.pl-log { list-style:none; margin:0; padding:10px 18px; border-top:1px solid var(--pline); font-size:11.5px; color:var(--pmuted);
  display:grid; grid-template-columns:minmax(0,1fr); gap:3px; max-height:104px; overflow:hidden }
.pl-log li { min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis } .pl-log li::before { content:"› "; color:var(--pblue) }
.pl-log li:last-child { color:var(--pfg) } .pl-log .who { color:var(--pblue) }
.pl-now { display:flex; flex-wrap:wrap; align-items:center; gap:6px 8px; padding:10px 18px; border-top:1px solid var(--pline); font-size:11.5px }
.pl-now .lbl { font-size:9.5px; letter-spacing:.11em; text-transform:uppercase; color:var(--pblue); margin-right:4px }
.pl-now .chip { border:1px solid var(--pline); background:var(--pbox); border-radius:999px; padding:2px 9px; color:var(--pfg) }
.pl-now .chip:last-of-type { border-color:#2c3a5c } .pl-now .cnt { color:var(--pmuted); margin-left:auto }
.context p { margin:0 0 6px } details.warnbox summary { color:var(--bad) }
.replaybar { margin:0 0 10px; padding:7px 12px; border-radius:8px; background:var(--warnbg); color:var(--warn); font:500 12.5px var(--mono) }
.pl-foot { display:flex; flex-wrap:wrap; gap:14px 34px; padding:12px 18px 14px; border-top:1px solid var(--pline) }
.pl-foot div { min-width:0 } .pl-foot span { display:block; font-size:9.5px; letter-spacing:.11em; text-transform:uppercase; color:var(--pmuted) }
.pl-foot b { display:block; font-weight:500; font-size:13.5px; margin-top:3px; font-variant-numeric:tabular-nums }
.pl-foot .stack { margin-left:auto; text-align:right } .pl-foot .stack b { color:var(--pblue); font-size:12px }
@keyframes plpulse { 0%,100% { opacity:1 } 50% { opacity:.35 } }
@keyframes plblink { 0%,100% { opacity:.25 } 40% { opacity:1 } }
@keyframes plflow { from { background-position:0 0 } to { background-position:6px 0 } }
@media (max-width:920px) { .pl-flow { grid-template-columns:minmax(0,1fr); gap:12px; padding:20px 14px 16px } .pl-head,.pl-log,.pl-foot { padding-left:14px; padding-right:14px } .node { display:grid; grid-template-columns:minmax(0,1fr) 104px; align-items:center; gap:10px; text-align:left }
  .node .box { padding:9px 12px; min-width:0 } .node .cap { margin:0; text-align:right; max-width:110px }
  .edge { top:100%; left:22px; width:4px; height:12px; background-size:4px 6px; background-repeat:repeat-y }
  .edge.flow { animation-name:plflowv } .pl-foot .stack { margin-left:0; text-align:left } }
@keyframes plflowv { from { background-position:0 0 } to { background-position:0 6px } }
@media (prefers-reduced-motion:reduce) { .pl * { animation:none !important } } .warnbox { background:var(--badbg); color:var(--bad); padding:6px 10px; border-radius:6px }

nav.top { display:flex; justify-content:space-between; align-items:center; gap:12px; font:600 16px var(--sans) }
nav.top a { font-weight:500; font-size:14px }
.title { font-size:17px; margin:4px 0 0 }
.status { display:inline-block; margin:10px 0 0; padding:6px 12px; border-radius:8px; font-weight:600; background:var(--code); color:var(--fg) }
.status.live,.status.done { background:var(--okbg); color:var(--ok) } .status.stopped { background:var(--badbg); color:var(--bad) }
.status.waiting { background:var(--warnbg); color:var(--warn) }
.todo { background:var(--surface); border:1px solid var(--line); border-left:4px solid var(--accent); border-radius:10px; padding:14px 16px }
.todo p { margin:6px 0 } .todo ul { margin:6px 0; padding-left:20px }
.cmd { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin:6px 0 } .cmd code { padding:6px 8px; overflow-wrap:anywhere }
.results { display:grid; gap:14px } .results:empty { display:none }
.pl-logw { border-top:1px solid var(--pline) } .pl-logw .lbl,.pl-now .lbl { font-size:9.5px; letter-spacing:.11em; text-transform:uppercase; color:var(--pblue) }
.pl-logw .lbl { display:block; padding:10px 18px 0 } .pl-logw .pl-log { border-top:0; padding-top:6px }
details.eng > summary { font-weight:600; font-size:15px; padding:8px 0; color:var(--fg) } .eng-body { display:grid; gap:18px; margin-top:8px }
'''


NODE_STATE = {"done": "done", "next": "running", "waiting for you": "waiting", "stopped": "stopped", "not reached": "pending"}
QUIET_S = 1200  # no new step or event for 20 min while "working": the process is gone (crash, closed terminal)
PHASE_WORD = {"starting": "getting ready", "could not start": "could not start", "working": "working",
              "interrupted": "interrupted",
              "waiting": "waiting for your OK", "stopped": "stopped", "done": "done", "crashed": "crashed",
              "idle": "not running"}
PHASE_CLASS = {"working": "live", "waiting": "waiting", "interrupted": "waiting", "done": "done", "stopped": "stopped",
               "could not start": "stopped", "crashed": "stopped"}
DOTS = '<span class="dots"><i></i><i></i><i></i></span>'


def _secs_since(d: dict, iso: str | None = None) -> float | None:
    try:
        return (datetime.fromisoformat(d["now"]) - datetime.fromisoformat(iso or d["since"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None


def inside(key: str, evs: list[dict]) -> tuple[list[str], str]:
    """What has happened inside one step, in plain words (newest last), and how many AI calls and test runs it made.
    Events are written when each thing ends, so every chip is something that happened, never a guess."""
    mine = [x for x in evs if x.get("step") == key]
    return [t for t in (plain.happened(x) for x in mine) if t][-6:], plain.counts(mine)


def where_now(d: dict, rows: list[dict], live: bool) -> dict:
    """Which step the run is at, and its phase: starting, working, waiting, stopped, done, crashed, could not start."""
    s, outcome, console = d["state"], d["state"].get("outcome") or {}, d.get("console") or ""
    states = [NODE_STATE[r["status"]] if live or r["status"] != "next" else "pending" for r in rows]
    cur = next((i for i, st in enumerate(states) if st in ("running", "waiting", "stopped")), None)
    if not s:
        phase = "could not start" if "PREFLIGHT FAIL" in console else "starting"
    elif live:
        quiet = [q for q in (_secs_since(d), _secs_since(d, d["events"][-1]["at"]) if d["events"] else None) if q is not None]
        phase = ("crashed" if "Traceback (most recent call last)" in console else
                 "interrupted" if quiet and min(quiet) > QUIET_S else "working")
    elif d["interrupt"]:
        phase = "waiting"
    elif outcome.get("exit") in GOOD_EXITS:
        phase = "done"
    elif outcome:
        phase = "stopped"
    else:
        phase = "idle"
    return {"states": states, "cur": cur, "phase": phase}


def headline(phase: str, cur: int | None, rows: list[dict]) -> str:
    step = f"step {cur + 1} of 9: {rows[cur]['label']}" if cur is not None else ""
    return {"starting": "Getting ready: checking that everything it needs is running",
            "could not start": "Could not start",
            "working": f"Working on {step}" if step else "Working",
            "waiting": "Waiting for your OK",
            "stopped": f"Stopped at {step}" if step else "Stopped",
            "done": "Done: all 9 steps finished",
            "crashed": f"Crashed during {step}" if step else "Crashed",
            "interrupted": f"Interrupted during {step}" if step else "Interrupted",
            "idle": "Not running"}[phase]


def pipeline(d: dict, rows: list[dict], live: bool, stats: list[tuple[str, str]]) -> str:
    """The run as a pipeline: all 9 steps, which one it is on, what has happened in it, and the latest activity."""
    w = where_now(d, rows, live)
    states, cur, phase = w["states"], w["cur"], w["phase"]
    secs = {x["step"]: x.get("seconds") for x in d["events"] if x.get("kind") == "step"}
    mode = ("replay · " if d.get("replay") else "") + PHASE_WORD[phase]
    if cur is not None:
        el = _secs_since(d) if states[cur] == "running" else None
        where = f"Step {cur + 1} of 9: {rows[cur]['label']}" + (f" · {plain.duration(el)}" if el is not None and el >= 0 else "")
    else:
        n = sum(st == "done" for st in states)
        where = "All 9 steps done" if n == 9 else f"{n} of 9 steps done"
    gives = {k: g for k, _, g in plain.STEPS}
    nodes = []
    for i, (r, st) in enumerate(zip(rows, states)):
        t = secs.get(r["key"])
        sub = {"done": f"✓ {plain.duration(t)}" if t is not None else "✓ done", "running": DOTS,
               "waiting": "waiting for you", "stopped": "stopped here", "pending": ""}[st]
        edge = ("" if i == len(rows) - 1 else
                f'<span class="edge {"flow" if i + 1 == cur and states[cur] == "running" else "lit" if st == "done" else ""}"></span>')
        nodes.append(_k(f"node-{i}", f'<li class="node {st}"><div class="box"><div class="k">Step {i + 1}</div>'
                     f'<div class="t">{e(r["label"])}</div><div class="s">{sub if st == "running" else e(sub)}</div></div>'
                     f'<div class="cap">{e(gives[r["key"]])}</div>{edge}</li>'))
    at = cur if cur is not None else max((i for i, st in enumerate(states) if st == "done"), default=None)
    if at is None:
        now = '<span class="lbl">So far</span><span class="chip">Nothing has happened yet</span>'
    else:
        chips, cnt = inside(rows[at]["key"], d["events"])
        if rows[at]["key"] == "approval" and d["interrupt"]:
            chips.append("Pull request text saved and locked")
        lbl = {"running": "So far in this step", "waiting": "At this step", "stopped": "Where it stopped",
               "done": "Last step"}[states[at]]
        now = (f'<span class="lbl">{e(lbl)}</span>' + "".join(f'<span class="chip">{e(c)}</span>' for c in chips)
               + (f'<span class="cnt">{e(cnt)}</span>' if cnt else "")
               + ("" if chips or cnt else '<span class="chip">Nothing logged yet</span>'))
    label = {k: lab for k, lab, _ in plain.STEPS}
    log = "".join(f'<li><span class="at">{e(x["at"][11:19])}</span> <span class="who">{e(label.get(x.get("step"), x.get("step")))}</span> '
                  f'{e(plain.activity(x))}</li>' for x in d["events"][-5:])
    foot = "".join(_k(f"stat-{i}", f'<div><span>{e(k)}</span><b>{e(v)}</b></div>') for i, (k, v) in enumerate(stats))
    return f"""<section class="pl {PHASE_CLASS.get(phase, 'pending')}" data-kc="pl" aria-label="Where the run is">
  <div class="pl-head">{_k("mode", f'<span><i class="pl-dot"></i>{e(plain.NAME)} · {e(mode)}</span>')}{_k("where", f'<span>{e(where)}</span>')}</div>
  <ol class="pl-flow">{"".join(nodes)}</ol>
  {_k("now", f'<div class="pl-now">{now}</div>')}
  {_k("log", f'<div class="pl-logw"><span class="lbl">Latest activity</span><ul class="pl-log">{log or "<li>Nothing yet</li>"}</ul></div>')}
  <div class="pl-foot">{foot}</div>
</section>"""


def _k(key: str, markup: str) -> str:
    """Mark a piece of the page for in-place updates: the served page swaps it only when its hash changes."""
    h = hashlib.sha1(markup.encode()).hexdigest()[:10]
    i = markup.index(">")
    return f'{markup[:i]} data-k="{key}" data-h="{h}"{markup[i:]}'


def _cmd(text: str) -> str:
    return f'<div class="cmd"><code>{e(text)}</code><button type="button" data-copy="{e(text)}">Copy</button></div>'


def render(d: dict, mode: str = "file", replay: dict | None = None) -> str:
    """mode "file": a static page (reloads itself while live). "live" / "replay": served by server.py, which the page
    asks every 2 s (replay: 0.7 s) and swaps in only the pieces that changed. The page can't approve anything."""
    s, m, rid = d["state"], d["meter"], d["run_id"]
    issue = s.get("issue") or {}
    outcome = s.get("outcome") or {}
    live = bool(d["next"]) and not d["interrupt"] and not outcome
    rows = step_rows(d)
    w = where_now(d, rows, live)
    phase, cur = w["phase"], w["cur"]
    served = mode in ("live", "replay")
    final = (bool(outcome) or phase == "could not start") if mode == "live" else \
        bool((replay or {}).get("final")) if mode == "replay" else not live

    clock = s.get("fix_clock") or {}
    if clock.get("validated"):
        proven = f"{plain.duration(clock['seconds'])} (2 tests pass)"
    elif clock.get("judges") == 1:
        proven = "not proven (only 1 test passed)"
    elif clock.get("seconds") is not None:
        proven = "no fix passed"
    elif outcome or phase in ("could not start", "crashed"):
        proven = "no fix"
    elif clock.get("started_at"):
        proven = f"{plain.duration(_secs_since(d, clock['started_at']))} so far"
    else:
        proven = "not started"
    spent, cap = m.get("spent_usd", 0) or 0, m.get("cap_usd", 0) or 0
    money = (f"${spent:.4f}" if 0 < spent < 0.1 else f"${spent:.2f}") + f" of ${cap:.2f} limit"
    machine = f"{m.get('sandbox_used_s', 0) or 0} s of {int(m.get('sandbox_cap_s') or 1800) // 60} min limit"
    fresh = freshness(d)
    stats = [("Time to a proven fix", proven), ("Money spent", money), ("Test machine time", machine),
             ("Last activity", "nothing yet" if fresh == "no events yet" else fresh)]

    # ── header: what this is and where it is, in one line ──
    rp = replay or {}
    who = f"{issue.get('owner', '')}/{issue.get('repo', '')}".strip("/")
    model = "Claude Opus" if s.get("demo") else "standard AI"
    head = f"""<header>
  {f'<p class="replaybar">Replay of a past run, sped up {rp.get("speed", 8):g}× (long waits cut to 4 s). Nothing is running. {rp.get("elapsed", 0):.0f} of {rp.get("length", 0):.0f} s · <a href="?speed={rp.get("speed", 8):g}">start again</a></p>' if mode == "replay" else ''}
  <h1>{f'Issue #{e(issue.get("number"))} in {e(who)}' if issue else 'New run'}</h1>
  {f'<p class="title">{e(issue.get("title"))}</p>' if issue.get('title') else ''}
  <p class="status {PHASE_CLASS.get(phase, '')}">{e(headline(phase, cur, rows))}</p>
  <p class="sub">Run {e(rid)} · {e(model)}{f' · <a href="{e(s.get("issue_url"))}">open the issue on GitHub</a>' if s.get('issue_url') else ''}{' · this page updates by itself' if served and not final else ''}<span class="offline" hidden> · lost connection to the viewer</span></p>
</header>"""

    # ── what the operator needs to do now ──
    approve = f"cd ~/Projects/DebugAssist && uv run debug-assist approve {rid}"
    reject = f"cd ~/Projects/DebugAssist && uv run debug-assist reject {rid}"
    resume = f"cd ~/Projects/DebugAssist && uv run debug-assist resume {rid}"
    why = str(outcome.get("why") or "")
    fails = [l.split("PREFLIGHT FAIL", 1)[1].strip() for l in (d.get("console") or "").splitlines() if "PREFLIGHT FAIL" in l]
    if mode == "replay":
        todo = "<p>Nothing. This is a recording of a past run.</p>"
    elif phase == "starting":
        todo = "<p>Nothing yet. It is checking that everything it needs is running.</p>"
    elif phase == "could not start":
        todo = ("<p>It could not start. Fix these, then start again:</p><ul>" + "".join(f"<li>{e(x)}</li>" for x in fails)
                + "</ul>")
    elif phase == "working":
        todo = "<p>Nothing. Watch the steps. This page updates by itself.</p>"
    elif phase == "crashed":
        todo = "<p>It crashed. To continue from the last finished step, run this in your terminal:</p>" + _cmd(resume)
    elif phase == "interrupted":
        todo = (f"<p>It is not running any more: nothing has happened for {e(freshness(d).replace(' ago', ''))}. "
                "To continue from the last finished step, run this in your terminal:</p>" + _cmd(resume))
    elif phase == "waiting":
        todo = ("<p>Read the pull request text below. If it is right, approve it by running this in your terminal:</p>"
                + _cmd(approve) + "<p>To say no instead:</p>" + _cmd(reject)
                + "<p class=\"note\">Approving only saves a script on your computer. Nothing is posted to GitHub.</p>")
    elif phase == "stopped":
        todo = (f"<p>{e(plain.exit_text(outcome.get('exit')))} There is nothing to approve.</p>"
                + (f'<p><a href="/">Start a new run</a></p>' if served else "")
                + (f"<details><summary>Technical reason</summary><p class=\"note\">{e(why)}</p></details>" if why else ""))
    elif phase == "done":
        todo = (f"<p>{e(plain.exit_text(outcome.get('exit')))}</p><p>The text is in <code>runs/{e(rid)}/PR.md</code>. "
                f"Nothing has been posted to GitHub. To post it, run <code>runs/{e(rid)}/publish.sh</code>: it shows you "
                f"the exact command, and nothing is sent until you run that command yourself.</p>")
    else:
        todo = "<p>Nothing. The run is not running.</p>"
    todo = f'<section class="todo"><h2>What you need to do</h2>{todo}</section>'

    problem = (s.get("focus") or issue.get("title") or "").strip()
    context = (f'<section class="card"><h2>The problem it is fixing</h2><p>{e(problem[:600])}</p></section>'
               if problem else '<section class="card" hidden></section>')

    story = (s.get("second_story") or {}).get("text", "")
    results = '<section class="results">'
    if story:
        results += '<div class="card"><h2>Why the bug slipped through</h2><div class="md" id="story"></div></div>'
    if d["pr_text"]:
        lock = ("" if d["pr_matches"] is not False else
                '<p class="warnbox">Warning: this file was changed after it was locked. Approval will be refused.</p>')
        results += f'<div class="card"><h2>The pull request text</h2>{lock}<div class="md" id="pr"></div></div>'
    results += "</section>"

    # ── details for engineers (folded) ──
    timeline = "".join(
        f'<li class="st {r["status"].replace(" ", "-")}"><b>{i + 1}. {e(r["label"])}</b>'
        f'<span class="tag">{e("running" if live and r["status"] == "next" else r["status"])}</span>'
        f'<span class="when">{e(r["span"])}</span>' + "".join(f"<p>{e(l[:260])}</p>" for l in r["lines"][-2:]) + "</li>"
        for i, r in enumerate(rows))
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
    trials = (f'<p class="note" style="margin-top:10px">Trials on this run (scripts that re-ran one step; never counted in the north stars):</p><ul class="cases">'
              + "".join(f"<li>{e(k)}: {v['n']} events ({e(', '.join(v['kinds']))})</li>" for k, v in sorted(d.get('trials', {}).items()))
              + '</ul>') if d.get('trials') else ''
    work = f'''<section class="grid2">
  <div class="card"><h2>Reproduce · the ladder</h2>
    <p class="note">{e(r.get('status', ''))} at rung <b>{e(r.get('rung', ''))}</b> · {confirm} · {e(r.get('attempts_used', ''))} attempt(s)</p>
    <div class="scroll"><table><tr><th>#</th><th>rung</th><th>result</th><th>test</th><th>what decided it</th></tr>{att_rows}</table></div>
    <p class="note">Judge for the fix: <code>{e(Path(r.get('oracle_test') or '').name)}</code></p></div>
  <div class="card"><h2>Cause and fix</h2>
    <p><code>{e(c.get('file', ''))}</code> lines {e('-'.join(map(str, c.get('lines', []))))}</p><p class="note">{e(c.get('why', ''))}</p>
    <div class="scroll"><table><tr><th>#</th><th>result</th><th>changed</th><th>suites</th><th>evidence</th></tr>{fix_rows}</table></div>
    <p class="note">Second test, written without seeing the fix: <b>{e(ho.get('status', 'not run'))}</b> <code>{e(Path(ho.get('test') or '').name)}</code></p>
    {f'<details><summary>Patch</summary><pre class="diff">{_diff(d["patch"])}</pre></details>' if d['patch'] else ''}</div>
</section>'''
    lessons = f'''<section class="grid2">
  <div class="card"><h2>Lasting guard</h2><p>{e(g.get('covers', ''))}</p>
    <p class="note">Laya: {e(g.get('a1_class', ''))} (p={e(g.get('a1_p', ''))}) · on the fixed code: ✓ closed {len(of.get('passed', []))} · ✗ still open {len(of.get('failed', []))} · ? broken {len(of.get('broken', []))}</p>
    <details><summary>Cases on the fixed code</summary><ul class="cases">{cases}</ul></details>
    <p class="note" style="margin-top:10px">Same code in {len(g.get('siblings', []))} other file(s), not fixed here:</p><ul class="cases">{sib}</ul>
    <p class="note">Named condition (frozen before the guard): {e((s.get('condition') or {}).get('text', ''))}</p></div>
  <div class="card"><h2>Back-test · 🎯 would have caught</h2>
    <p><b>{e(_wouldve(b))}</b>. Self-check at the anchor <code>{e((det.get('anchor') or {}).get('sha', ''))}</code>: {e(caught)} (in-sample: the guard was written from this bug)</p>
    <p class="note">On the {e(fa.get('window', '?'))} commits before: false alarms {e(fa.get('fired', '?'))} · quiet {e(fa.get('quiet', '?'))} · bug already there {e(fa.get('bug_already_there', '?'))} · unevaluable {e(fa.get('unevaluable', '?'))}</p>
    <div class="scroll"><table><tr><th>commit</th><th>date</th><th>covers</th><th>result</th><th>judge</th><th>guard</th><th>title</th></tr>{groups}</table></div></div>
</section>'''
    spend = f'''<section class="grid2">
  <div class="card"><h2>Spend, call by call</h2><div class="scroll"><table><tr><th>at</th><th>step</th><th>model</th><th>in</th><th>out</th><th>cost</th><th>status</th></tr>{calls}</table></div></div>
  <div class="card"><h2>Event log</h2><details><summary>{len(d['events'])} events</summary><div class="scroll"><table>{evs}</table></div></details>{trials}</div>
</section>'''
    md = json.dumps({"story": story, "pr": d["pr_text"]}).replace("</", "<\\/")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{'<meta http-equiv="refresh" content="3">' if live and not served else ''}
<title>{plain.NAME}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.1.6/purify.min.js"></script>
<style>{_CSS}</style></head><body><main>
{f'<nav class="top"><b>{plain.NAME}</b><a href="/">All runs · start a new run</a></nav>' if served else f'<nav class="top"><b>{plain.NAME}</b></nav>'}
{_k("head", head)}
{pipeline(d, rows, live, stats)}
{_k("todo", todo)}
{_k("context", context)}
{_k("results", results)}
{_k("final", f'<i hidden data-final="{int(final)}"></i>')}
<details class="eng"><summary>Details for engineers</summary><div class="eng-body">
{_k("steps", f'<section><h2>Step log</h2><ol class="tl">{timeline}</ol></section>')}
{_k("work", work)}
{_k("lessons", lessons)}
{_k("spend", spend)}
<p class="note">Read-only. Built from this run's MongoDB state, event log and spend meter.</p>
</div></details>
</main>
{_k("md", f'<script type="application/json" id="md">{md}</script>')}
<script>
const show = (id, text) => {{
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = "";
  if (!text) {{ el.textContent = "Not written yet."; return; }}
  if (window.marked && window.DOMPurify) el.innerHTML = DOMPurify.sanitize(marked.parse(text));
  else {{ const pre = document.createElement("pre"); pre.textContent = text; el.appendChild(pre); }}
}};
const renderMd = () => {{ const MD = JSON.parse(document.getElementById("md").textContent); show("story", MD.story); show("pr", MD.pr); }};
renderMd();
document.addEventListener("click", ev => {{
  const b = ev.target.closest("[data-copy]");
  if (b) navigator.clipboard.writeText(b.dataset.copy).then(() => {{ b.textContent = "Copied"; }});
}});
{_LIVE_JS.replace("__MODE__", mode) if served else ''}
</script></body></html>"""


# Served pages only: ask the server for the page again and swap in the pieces whose hash changed. GET only, same
# origin; replay passes how far in it is (?ms=). Stops when the run is final; shows "viewer offline" if the server goes.
_LIVE_JS = """
const MODE = "__MODE__", T0 = Date.now();
const offline = on => document.querySelectorAll(".offline").forEach(x => x.hidden = !on);
async function poll() {
  const u = new URL(location.href);
  if (MODE === "replay") u.searchParams.set("ms", String(Date.now() - T0));
  let wait = MODE === "replay" ? 700 : 2000;
  try {
    const r = await fetch(u, { cache: "no-store" });
    if (!r.ok) throw new Error(String(r.status));
    const doc = new DOMParser().parseFromString(await r.text(), "text/html");
    let md = false;
    doc.querySelectorAll("[data-kc]").forEach(n => {
      const o = document.querySelector(`[data-kc="${n.dataset.kc}"]`);
      if (o && o.className !== n.className) o.className = n.className;
    });
    doc.querySelectorAll("[data-k]").forEach(n => {
      const o = document.querySelector(`[data-k="${n.dataset.k}"]`);
      if (o && o.dataset.h !== n.dataset.h) {
        o.replaceWith(document.importNode(n, true));
        if (["md", "results"].includes(n.dataset.k)) md = true;
      }
    });
    if (md) renderMd();
    offline(false);
    if (doc.querySelector('[data-final="1"]')) return;
  } catch (err) { offline(true); wait = 5000; }
  setTimeout(poll, wait);
}
if (!document.querySelector('[data-final="1"]')) setTimeout(poll, MODE === "replay" ? 300 : 2000);
"""


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
