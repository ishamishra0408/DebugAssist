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

STEPS = [("read_issue", "Read issue", "triage"), ("reproduce", "Reproduce", "repro"), ("find_cause", "Find cause", "cause"),
         ("write_fix", "Write fix", "fix"), ("why_it_shipped", "Why it shipped", "second_story"),
         ("lasting_guard", "Lasting guard", "guard"), ("test_past_bugs", "Past bugs", "backtest"),
         ("approval", "Your approval", "approval"), ("open_pr", "Open PR", "published")]
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
            "edited": (snap.metadata or {}).get("source") == "update"}  # state written outside the pipeline


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
KIND = {"read_issue": ("issue", "github · laya"), "reproduce": ("ladder", "{model} · docker"),
        "find_cause": ("cause", "{model} · git grep"), "write_fix": ("fix", "{model} · 2 judges"),
        "why_it_shipped": ("story", "git history · {model}"), "lasting_guard": ("guard", "{model} · laya a1"),
        "test_past_bugs": ("back-test", "qwen3 · vector search"), "approval": ("human", "you · sha256"),
        "open_pr": ("output", "publish.sh · echo only")}
NODE_STATE = {"done": "done", "next": "running", "waiting for you": "waiting", "stopped": "stopped", "not reached": "pending"}


def _secs_since(d: dict) -> float | None:
    try:
        return (datetime.fromisoformat(d["now"]) - datetime.fromisoformat(d["since"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None


def inside(key: str, evs: list[dict]) -> tuple[list[str], list[str]]:
    """Inside one step: its structured events as short chips (newest last) and counts of model calls and sandbox runs.
    Events are written when each thing ends, so a chip is something that has happened, never a guess at the present."""
    mine = [x for x in evs if x.get("step") == key]
    chips = []
    for x in mine:
        k = x["kind"]
        if k == "attempt":
            chips.append(f"{x.get('rung')} #{x.get('n')} {x.get('outcome')}")
        elif k == "fix_attempt":
            chips.append(f"fix #{x.get('n')} {'✓ validated' if x.get('ok') else '✗ not validated'}")
        elif k == "holdout":
            chips.append(f"second test: {x.get('on_fixed')} on the fix")
        elif k == "guard_try":
            chips.append(f"guard try #{x.get('n')}: {'accepted' if x.get('result') == 'accepted' else 'retry'}")
        elif k == "backtest":
            chips.append(f"{str(x.get('commit', ''))[:7]} {x.get('state')}")
        elif k == "embed":
            chips.append("condition embedded")
        elif k == "approval":
            chips.append(str(x.get("status")))
        elif k == "laya":
            chips.append("laya: " + _event_text(x)[:44])
    calls = sum(x["kind"] == "model_call" for x in mine)
    runs = sum(x["kind"] == "sandbox" for x in mine)
    counts = [f"{calls} model call{'s' * (calls != 1)}"] * bool(calls) + [f"{runs} sandbox run{'s' * (runs != 1)}"] * bool(runs)
    return chips[-6:], counts


def _k(key: str, markup: str) -> str:
    """Mark a piece of the page for in-place updates: the served page swaps it only when its hash changes."""
    h = hashlib.sha1(markup.encode()).hexdigest()[:10]
    i = markup.index(">")
    return f'{markup[:i]} data-k="{key}" data-h="{h}"{markup[i:]}'


def pipeline(d: dict, rows: list[dict], live: bool, stats: list[tuple[str, str]]) -> str:
    """The run as a live pipeline: which step we're on, what each is, and the newest events (read-only)."""
    s = d["state"]
    outcome = s.get("outcome") or {}
    model = "opus 5.5" if s.get("demo") else "dev model"
    secs = {x["step"]: x.get("seconds") for x in d["events"] if x.get("kind") == "step"}
    states = [NODE_STATE[r["status"]] if live or r["status"] != "next" else "pending" for r in rows]
    cur = next((i for i, st in enumerate(states) if st in ("running", "waiting", "stopped")), None)
    if d.get("replay") and live:
        mode, cls = "replay · running", "live"
    elif live:
        mode, cls = "live", "live"
    elif d["interrupt"]:
        mode, cls = "paused · your approval", "waiting"
    elif outcome.get("exit") in GOOD_EXITS:
        mode, cls = "done", "done"
    elif outcome:
        mode, cls = f"stopped · {outcome.get('exit', '').lower()}", "stopped"
    else:
        mode, cls = ("starting" if not s else "idle"), "pending"
    if d.get("edited"):
        mode += " · state edited outside the pipeline"
    if cur is not None:
        el = _secs_since(d) if states[cur] == "running" else None
        took = "" if el is None or el < 0 else f" · {el:.0f} s" if el < 90 else f" · {el / 60:.0f} min"
        where = f"step {cur + 1} of 9 · {rows[cur]['label']}{took}"
    else:
        where = f"{sum(st == 'done' for st in states)} of 9 steps done"
    nodes = []
    for i, (r, st) in enumerate(zip(rows, states)):
        kind, tech = KIND[r["key"]]
        sub = {"done": f"✓ {secs[r['key']]:.0f} s" if secs.get(r["key"]) is not None else "✓ done",
               "running": '<span class="dots"><i></i><i></i><i></i></span>', "waiting": "waiting for you",
               "stopped": "stopped", "pending": ""}[st]
        edge = ("" if i == len(rows) - 1 else
                f'<span class="edge {"flow" if i + 1 == cur and states[cur] == "running" else "lit" if st == "done" else ""}"></span>')
        nodes.append(_k(f"node-{i}", f'<li class="node {st}"><div class="box"><div class="k">{i + 1} · {e(kind)}</div>'
                     f'<div class="t">{e(r["label"])}</div><div class="s">{sub if st == "running" else e(sub)}</div></div>'
                     f'<div class="cap">{e(tech.format(model=model))}</div>{edge}</li>'))
    at = cur if cur is not None else max((i for i, st in enumerate(states) if st == "done"), default=None)
    if at is None:
        now = '<span class="lbl">now</span><span class="chip">waiting for the first step</span>'
    else:
        chips, counts = inside(rows[at]["key"], d["events"])
        if rows[at]["key"] == "approval" and d["interrupt"]:
            chips.append(f"PR draft fingerprinted · sha256 {str(d['interrupt'].get('sha256', ''))[:12]}")
        if cur is None and outcome:
            chips = [f"run ended · {outcome.get('exit')}"] + chips
        verb = {"running": "inside", "waiting": "waiting at", "stopped": "stopped in", "done": "last step"}[states[at]]
        now = (f'<span class="lbl">now · {e(verb)} {e(rows[at]["label"])}</span>'
               + "".join(f'<span class="chip">{e(c)}</span>' for c in chips)
               + (f'<span class="cnt">{e(" · ".join(counts))}</span>' if counts else "")
               + ("" if chips or counts else '<span class="chip">nothing logged yet</span>'))
    log = "".join(f'<li><span class="at">{e(x["at"][11:19])}</span> <span class="who">{e(x.get("step"))}</span> '
                  f'{e(x["kind"])} · {e(_event_text(x)[:110])}</li>' for x in d["events"][-5:])
    foot = "".join(_k(f"stat-{i}", f'<div><span>{e(k)}</span><b>{e(v)}</b></div>') for i, (k, v) in enumerate(stats))
    return f"""<section class="pl {cls}" data-kc="pl" aria-label="Pipeline status">
  <div class="pl-head">{_k("mode", f'<span><i class="pl-dot"></i>debug assist pipeline · {e(mode)}</span>')}{_k("where", f'<span>{e(where)}</span>')}</div>
  <ol class="pl-flow">{"".join(nodes)}</ol>
  {_k("now", f'<div class="pl-now">{now}</div>')}
  {_k("log", f'<ul class="pl-log">{log or "<li>no events yet</li>"}</ul>')}
  <div class="pl-foot">{foot}<div class="stack"><span>stack</span><b>LangGraph · MongoDB · Docker · Phoenix</b></div></div>
</section>"""


def render(d: dict, mode: str = "file", replay: dict | None = None) -> str:
    """mode "file": a static page (reloads itself while live). "live" / "replay": served by server.py, which the page
    asks every 2 s (replay: 0.7 s) and swaps in only the pieces that changed (GET only; the page still can't act)."""
    s, m = d["state"], d["meter"]
    issue = s.get("issue") or {}
    outcome = s.get("outcome") or {}
    live = bool(d["next"]) and not d["interrupt"] and not outcome
    good = outcome.get("exit") in GOOD_EXITS
    status = (outcome.get("exit") if good else f"STOPPED · {outcome.get('exit')}" if outcome else "Waiting for your approval" if d["interrupt"]
              else "Running" if live else "Finished" if s else "Starting")
    clock = s.get("fix_clock") or {}
    judges = clock.get("judges")
    clock_txt = (f"{clock['seconds']:.0f} s" if clock.get("seconds") is not None else "—")
    clock_sub = ("validated · two independent tests" if clock.get("validated") else
                 "one judge only: does not count" if judges == 1 else "not validated" if clock.get("seconds") else "clock running")
    spent, cap = m.get("spent_usd", 0) or 0, m.get("cap_usd", 0) or 0
    pct = min(100, (spent / cap * 100) if cap else 0)
    model = "Claude Opus (demo)" if s.get("demo") else "dev model"

    rows = step_rows(d)
    stats = [("⏱ fix clock", f"{clock_txt} · {clock_sub}"), ("spent", f"${spent:.2f} of ${cap:.2f}"),
             ("sandbox", f"{m.get('sandbox_used_s', 0)} of {m.get('sandbox_cap_s', '?')} s"),
             ("newest event", f"{freshness(d)} · {len(d['events'])} events")]
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

    intr = d["interrupt"]
    approve = f"cd ~/Projects/DebugAssist && uv run debug-assist approve {d['run_id']}"
    pr_check = ("fingerprint matches the text below" if d["pr_matches"] else
                "THE FILE ON DISK DIFFERS FROM THE TEXT THAT WAS FINGERPRINTED" if d["pr_matches"] is False else "")
    md = json.dumps({"story": (s.get("second_story") or {}).get("text", ""), "pr": d["pr_text"]}).replace("</", "<\\/")

    served = mode in ("live", "replay")
    final = bool(outcome) if mode == "live" else bool((replay or {}).get("final")) if mode == "replay" else not live
    rp = replay or {}
    head = f"""<header>
  {f'<p class="replaybar">REPLAY of a recorded run, from its checkpoints and event log · recorded {e(d["run_id"][-15:-7])} · {rp.get("speed", 8):g}× with waits over 4 s shortened · {rp.get("elapsed", 0):.0f} of {rp.get("length", 0):.0f} s · <a href="?speed={rp.get("speed", 8):g}">restart</a></p>' if mode == "replay" else ''}
  <h1>{e(issue.get('repo', ''))}#{e(issue.get('number', ''))}: {e(issue.get('title', '') or ('starting…' if not s else ''))}</h1>
  <p class="sub"><span class="badge {'running' if (live or good) else 'stopped' if outcome else ''}">{e(status)}</span>
   · run <code>{e(d['run_id'])}</code> · {e(model)} · <a href="{e(s.get('issue_url', ''))}">issue</a> · {'updates in place' if served else 'built ' + e(d['built'])}{' · refreshing' if live and not served else ''}<span class="offline" hidden> · viewer offline</span></p>
</header>"""
    why = str(outcome.get("why") or "")
    context = (f'<section class="context">'
               + (f'<p class="sub"><b>Focus:</b> {e((s.get("focus") or "")[:400])}</p>' if s.get("focus") else "")
               + ((f'<p class="note">{e(why)}</p>' if good else
                   f'<p class="warnbox">{e(why)}</p>' if len(why) <= 240 else
                   f'<details class="warnbox"><summary>{e(why[:200])}…</summary>{e(why[200:])}</details>') if outcome else "")
               + "</section>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{'<meta http-equiv="refresh" content="3">' if live and not served else ''}
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
main {{ max-width:1180px; margin:0 auto; display:grid; grid-template-columns:minmax(0,1fr); gap:18px }} main > * {{ min-width:0 }}
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
.note {{ color:var(--muted); font-size:13px }}
.pl {{ --pbg:#0a0c10; --pbox:#12161c; --pline:#1f252d; --pfg:#e6e9ee; --pmuted:#7b8494; --pblue:#5b84ff; --pgreen:#3ecf8e;
  --pamber:#e5a53c; --pred:#f06b60; background:var(--pbg); color:var(--pfg); border:1px solid var(--pline); border-radius:14px;
  overflow:hidden; font-family:var(--mono) }}
.pl-head {{ display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; padding:12px 18px; border-bottom:1px solid var(--pline);
  font-size:11px; letter-spacing:.09em; text-transform:uppercase; color:var(--pmuted) }}
.pl-dot {{ display:inline-block; width:7px; height:7px; border-radius:50%; margin-right:9px; background:var(--pmuted); vertical-align:1px }}
.pl.live .pl-dot,.pl.done .pl-dot {{ background:var(--pgreen) }} .pl.live .pl-dot {{ animation:plpulse 1.6s ease-in-out infinite }}
.pl.waiting .pl-dot {{ background:var(--pamber) }} .pl.stopped .pl-dot {{ background:var(--pred) }}
.pl-flow {{ list-style:none; margin:0; padding:30px 18px 22px; display:grid; grid-template-columns:repeat(9,minmax(0,1fr)); gap:20px }}
.node {{ position:relative; min-width:0; text-align:center }}
.node .box {{ position:relative; background:var(--pbox); border:1px solid var(--pline); border-radius:10px; padding:10px 6px 9px;
  transition:border-color .3s, box-shadow .3s }}
.node .box::after {{ content:""; position:absolute; top:7px; right:7px; width:6px; height:6px; border-radius:50% }}
.node .k {{ font-size:9.5px; letter-spacing:.11em; text-transform:uppercase; color:var(--pmuted) }}
.node .t {{ font:500 13px/1.25 var(--sans); margin-top:4px; color:var(--pfg); overflow-wrap:anywhere }}
.node .s {{ font-size:10.5px; margin-top:5px; color:var(--pmuted); min-height:14px }}
.node .cap {{ font-size:9.5px; color:var(--pmuted); margin-top:7px; opacity:.75; overflow-wrap:anywhere }}
.node.done .box {{ border-color:#1f3a2d }} .node.done .box::after {{ background:var(--pgreen) }} .node.done .s {{ color:var(--pgreen) }}
.node.running .box {{ border-color:var(--pblue); background:#0f1830; box-shadow:0 0 0 1px var(--pblue),0 0 26px rgba(91,132,255,.38) }}
.node.running .k,.node.running .cap {{ color:var(--pblue); opacity:1 }} .node.running .box::after {{ background:var(--pblue); animation:plpulse 1.2s infinite }}
.node.waiting .box {{ border-color:var(--pamber); box-shadow:0 0 22px rgba(229,165,60,.25) }} .node.waiting .box::after {{ background:var(--pamber) }}
.node.waiting .s {{ color:var(--pamber) }}
.node.stopped .box {{ border-color:var(--pred); box-shadow:0 0 18px rgba(240,107,96,.22) }} .node.stopped .box::after {{ background:var(--pred) }}
.node.stopped .s {{ color:var(--pred) }} .node.pending {{ opacity:.42 }}
.dots {{ display:inline-flex; gap:4px }} .dots i {{ width:5px; height:5px; border-radius:50%; background:var(--pblue); animation:plblink 1.2s infinite }}
.dots i:nth-child(2) {{ animation-delay:.2s }} .dots i:nth-child(3) {{ animation-delay:.4s }}
.edge {{ position:absolute; top:31px; left:100%; width:20px; height:4px; color:var(--pline);
  background-image:radial-gradient(circle,currentColor 1.3px,transparent 1.7px); background-size:6px 4px; background-repeat:repeat-x }}
.edge.lit {{ color:#2c6b52 }} .edge.flow {{ color:var(--pblue); animation:plflow .6s linear infinite }}
.pl-log {{ list-style:none; margin:0; padding:10px 18px; border-top:1px solid var(--pline); font-size:11.5px; color:var(--pmuted);
  display:grid; grid-template-columns:minmax(0,1fr); gap:3px; max-height:104px; overflow:hidden }}
.pl-log li {{ min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis }} .pl-log li::before {{ content:"› "; color:var(--pblue) }}
.pl-log li:last-child {{ color:var(--pfg) }} .pl-log .who {{ color:var(--pblue) }}
.pl-now {{ display:flex; flex-wrap:wrap; align-items:center; gap:6px 8px; padding:10px 18px; border-top:1px solid var(--pline); font-size:11.5px }}
.pl-now .lbl {{ font-size:9.5px; letter-spacing:.11em; text-transform:uppercase; color:var(--pblue); margin-right:4px }}
.pl-now .chip {{ border:1px solid var(--pline); background:var(--pbox); border-radius:999px; padding:2px 9px; color:var(--pfg) }}
.pl-now .chip:last-of-type {{ border-color:#2c3a5c }} .pl-now .cnt {{ color:var(--pmuted); margin-left:auto }}
.context p {{ margin:0 0 6px }} details.warnbox summary {{ color:var(--bad) }}
.replaybar {{ margin:0 0 10px; padding:7px 12px; border-radius:8px; background:var(--warnbg); color:var(--warn); font:500 12.5px var(--mono) }}
.pl-foot {{ display:flex; flex-wrap:wrap; gap:14px 34px; padding:12px 18px 14px; border-top:1px solid var(--pline) }}
.pl-foot div {{ min-width:0 }} .pl-foot span {{ display:block; font-size:9.5px; letter-spacing:.11em; text-transform:uppercase; color:var(--pmuted) }}
.pl-foot b {{ display:block; font-weight:500; font-size:13.5px; margin-top:3px; font-variant-numeric:tabular-nums }}
.pl-foot .stack {{ margin-left:auto; text-align:right }} .pl-foot .stack b {{ color:var(--pblue); font-size:12px }}
@keyframes plpulse {{ 0%,100% {{ opacity:1 }} 50% {{ opacity:.35 }} }}
@keyframes plblink {{ 0%,100% {{ opacity:.25 }} 40% {{ opacity:1 }} }}
@keyframes plflow {{ from {{ background-position:0 0 }} to {{ background-position:6px 0 }} }}
@media (max-width:920px) {{ .pl-flow {{ grid-template-columns:minmax(0,1fr); gap:12px; padding:20px 14px 16px }} .pl-head,.pl-log,.pl-foot {{ padding-left:14px; padding-right:14px }} .node {{ display:grid; grid-template-columns:minmax(0,1fr) 104px; align-items:center; gap:10px; text-align:left }}
  .node .box {{ padding:9px 12px; min-width:0 }} .node .cap {{ margin:0; text-align:right; max-width:110px }}
  .edge {{ top:100%; left:22px; width:4px; height:12px; background-size:4px 6px; background-repeat:repeat-y }}
  .edge.flow {{ animation-name:plflowv }} .pl-foot .stack {{ margin-left:0; text-align:left }} }}
@keyframes plflowv {{ from {{ background-position:0 0 }} to {{ background-position:0 6px }} }}
@media (prefers-reduced-motion:reduce) {{ .pl * {{ animation:none !important }} }} .warnbox {{ background:var(--badbg); color:var(--bad); padding:6px 10px; border-radius:6px }}
</style></head><body><main>
{_k("head", head)}
{pipeline(d, rows, live, stats)}
{_k("context", context)}
{_k("steps", f'<section><h2>Step log</h2><ol class="tl">{timeline}</ol></section>')}
{_k("final", f'<i hidden data-final="{int(final)}"></i>')}
{_k("work", f'''<section class="grid2">
  <div class="card"><h2>Reproduce · the ladder</h2>
    <p class="note">{e(r.get('status', ''))} at rung <b>{e(r.get('rung', ''))}</b> · {confirm} · {e(r.get('attempts_used', ''))} attempt(s)</p>
    <div class="scroll"><table><tr><th>#</th><th>rung</th><th>result</th><th>test</th><th>what decided it</th></tr>{att_rows}</table></div>
    <p class="note">Judge for the fix: <code>{e(Path(r.get('oracle_test') or '').name)}</code></p></div>
  <div class="card"><h2>Cause and fix</h2>
    <p><code>{e(c.get('file', ''))}</code> lines {e('-'.join(map(str, c.get('lines', []))))}</p><p class="note">{e(c.get('why', ''))}</p>
    <div class="scroll"><table><tr><th>#</th><th>result</th><th>changed</th><th>suites</th><th>evidence</th></tr>{fix_rows}</table></div>
    <p class="note">Second test, written without seeing the fix: <b>{e(ho.get('status', 'not run'))}</b> <code>{e(Path(ho.get('test') or '').name)}</code></p>
    {f'<details><summary>Patch</summary><pre class="diff">{_diff(d["patch"])}</pre></details>' if d['patch'] else ''}</div>
</section>''')}
{_k("story", f'''<section class="card"><h2>Why it shipped · conditions, not people</h2><div class="md" id="story"></div>
  <p class="note">Named condition (frozen before the guard): {e((s.get('condition') or {}).get('text', ''))}</p></section>''')}
{_k("lessons", f'''<section class="grid2">
  <div class="card"><h2>Lasting guard</h2><p>{e(g.get('covers', ''))}</p>
    <p class="note">Laya: {e(g.get('a1_class', ''))} (p={e(g.get('a1_p', ''))}) · on the fixed code: ✓ closed {len(of.get('passed', []))} · ✗ still open {len(of.get('failed', []))} · ? broken {len(of.get('broken', []))}</p>
    <details><summary>Cases on the fixed code</summary><ul class="cases">{cases}</ul></details>
    <p class="note" style="margin-top:10px">Same code in {len(g.get('siblings', []))} other file(s), not fixed here:</p><ul class="cases">{sib}</ul></div>
  <div class="card"><h2>Back-test · 🎯 would have caught</h2>
    <p><b>{e(_wouldve(b))}</b>. Self-check at the anchor <code>{e((det.get('anchor') or {}).get('sha', ''))}</code>: {e(caught)} (in-sample: the guard was written from this bug)</p>
    <p class="note">On the {e(fa.get('window', '?'))} commits before: false alarms {e(fa.get('fired', '?'))} · quiet {e(fa.get('quiet', '?'))} · bug already there {e(fa.get('bug_already_there', '?'))} · unevaluable {e(fa.get('unevaluable', '?'))}</p>
    <div class="scroll"><table><tr><th>commit</th><th>date</th><th>covers</th><th>result</th><th>judge</th><th>guard</th><th>title</th></tr>{groups}</table></div></div>
</section>''')}
{_k("pr", f'''<section class="card"><h2>The PR draft you approve</h2>
  {f'<p class="note">sha256 <code>{e(str(intr.get("sha256", ""))[:12])}</code> · {e(pr_check)}</p>' if intr else ''}
  {f'<div class="approve"><code id="cmd">{e(approve)}</code><button onclick="navigator.clipboard.writeText(document.getElementById(&quot;cmd&quot;).textContent)">Copy approve command</button></div><p class="note">Approving writes publish.sh, which only prints a command; nothing is posted.</p>' if intr else ''}
  <div class="md" id="pr"></div></section>''')}
{_k("spend", f'''<section class="grid2">
  <div class="card"><h2>Spend, call by call</h2><div class="scroll"><table><tr><th>at</th><th>step</th><th>model</th><th>in</th><th>out</th><th>cost</th><th>status</th></tr>{calls}</table></div></div>
  <div class="card"><h2>Event log</h2><details><summary>{len(d['events'])} events</summary><div class="scroll"><table>{evs}</table></div></details>
    {f'<p class="note" style="margin-top:10px">Trials on this run (scripts that re-ran one step; never counted in the north stars):</p><ul class="cases">' + "".join(f"<li>{e(k)}: {v['n']} events ({e(', '.join(v['kinds']))})</li>" for k, v in sorted(d.get('trials', {}).items())) + '</ul>' if d.get('trials') else ''}</div>
</section>''')}
<p class="note">Read-only. Built from this run's MongoDB state, event log and spend meter by <code>debug-assist {'serve' if served else 'view'}</code>.</p>
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
        if (["md", "story", "pr"].includes(n.dataset.k)) md = true;
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
