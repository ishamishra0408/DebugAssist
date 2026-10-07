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

from . import chart, icons, plain

_FIELD = {"read_issue": "triage", "gather_context": "context", "reproduce": "repro", "find_cause": "cause", "write_fix": "fix",
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
    pack = {}
    cpath = ((snap.values or {}).get("context") or {}).get("path")
    if cpath and Path(cpath).exists():
        try:
            pack = json.loads(Path(cpath).read_text())
        except ValueError:
            pack = {}
    return {"run_id": run_id, "state": snap.values or {}, "next": list(snap.next or []), "interrupt": intr, "pack": pack,
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
    later_done = False
    for r in reversed(out):  # a step with no output before steps that have output: added after this run
        if r["status"] == "done":
            later_done = True
        elif later_done and r["status"] == "not reached":
            r["status"] = "skipped"
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
STATIC = Path(__file__).parent / "static"
NODE_STATE = {"done": "done", "next": "running", "waiting for you": "waiting", "stopped": "stopped", "not reached": "pending",
              "skipped": "skipped"}
QUIET_S = 1200  # no new step or event for 20 min while "working": the process is gone (crash, closed terminal)
PHASE_CLASS = {"working": "s-live", "waiting": "s-waiting", "interrupted": "s-waiting", "done": "s-done",
               "stopped": "s-stopped", "could not start": "s-stopped", "crashed": "s-stopped"}


def _secs_since(d: dict, iso: str | None = None) -> float | None:
    try:
        return (datetime.fromisoformat(d["now"]) - datetime.fromisoformat(iso or d["since"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None


def inside(key: str, evs: list[dict]) -> tuple[list[str], str]:
    """What has happened inside one step, in plain words (newest last), and how many AI calls and test runs it made.
    Events are written when each thing ends, so every item is something that happened, never a guess."""
    mine = [x for x in evs if x.get("step") == key]
    return [t for t in (plain.happened(x) for x in mine) if t][-6:], plain.counts(mine)


def where_now(d: dict, rows: list[dict], live: bool) -> dict:
    """Which step the run is at, and its phase: starting, working, waiting, stopped, done, crashed, interrupted,
    could not start."""
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
    step = f"step {cur + 1} of {len(rows)}: {rows[cur]['label']}" if cur is not None else ""
    return {"starting": "Getting ready",
            "could not start": "Could not start",
            "working": f"Working on {step}" if step else "Working",
            "waiting": "Waiting for your OK",
            "stopped": f"Stopped at {step}" if step else "Stopped",
            "done": f"Done. All {len(rows)} steps finished.",
            "crashed": f"Crashed during {step}" if step else "Crashed",
            "interrupted": f"Interrupted during {step}" if step else "Interrupted",
            "idle": "Not running"}[phase]


def _k(key: str, markup: str) -> str:
    """Mark a piece of the page for in-place updates: the served page swaps it only when its hash changes."""
    h = hashlib.sha1(markup.encode()).hexdigest()[:10]
    i = markup.index(">")
    return f'{markup[:i]} data-k="{key}" data-h="{h}"{markup[i:]}'


def _since(d: dict, iso: str | None, replay: bool, suffix: str = "") -> str:
    """A running clock. Live: the browser counts it (same machine), so the page's pieces don't change every second.
    Replay: the server's recorded moment."""
    if not iso:
        return ""
    if replay:
        return e(plain.duration(_secs_since(d, iso)) + suffix)
    return f'<span data-since="{e(iso)}" data-suffix="{e(suffix)}"></span>'


def _copy(text: str, label: str, prominent: bool = False) -> str:
    short = label.removesuffix(" command")  # phones show "Copy approve"; the spoken label stays whole
    return (f'<button type="button" class="btn glass{" prominent" if prominent else ""}" data-copy="{e(text)}" '
            f'data-done="Copied" aria-label="{e(label)}">{icons.copy()}<span>{e(short)}<span class="wide"> command</span></span>'
            f'</button>' if short != label else
            f'<button type="button" class="btn glass{" prominent" if prominent else ""}" data-copy="{e(text)}" '
            f'data-done="Copied">{icons.copy()}<span>{e(label)}</span></button>')


def _link(href: str, label: str, icon: str = "", prominent: bool = False) -> str:
    return f'<a class="btn glass{" prominent" if prominent else ""}" href="{e(href)}">{icon}<span>{e(label)}</span></a>'


def _runs_dir() -> Path:
    from .config import CFG
    return CFG.runs_dir


def render(d: dict, mode: str = "file", replay: dict | None = None) -> str:
    """mode "file": a static page (reloads itself while live). "live" / "replay": served by server.py, which the page
    asks every 2 s (replay: 0.7 s) and swaps in only the pieces that changed. The page can't approve anything."""
    s, m, rid = d["state"], d["meter"], d["run_id"]
    issue = s.get("issue") or {}
    outcome = s.get("outcome") or {}
    live = bool(d["next"]) and not d["interrupt"] and not outcome
    rows = step_rows(d)
    w = where_now(d, rows, live)
    phase, cur, states = w["phase"], w["cur"], w["states"]
    served, is_replay = mode in ("live", "replay"), mode == "replay"
    final = (bool(outcome) or phase == "could not start") if mode == "live" else \
        bool((replay or {}).get("final")) if is_replay else not live
    cls = PHASE_CLASS.get(phase, "s-idle")
    rp = replay or {}
    secs = {x["step"]: x.get("seconds") for x in d["events"] if x.get("kind") == "step"}
    gives = {k: g for k, _, g in plain.STEPS}

    # ── header: what this is, where it is, all 9 steps ──
    who = f"{issue.get('owner', '')}/{issue.get('repo', '')}".strip("/")
    model = "Claude Opus" if s.get("demo") else "Standard AI"
    running_since = d.get("since") if cur is not None and states[cur] == "running" else None
    nodes = "".join(
        f'<li class="node {st}"><span class="bead">{icons.STATE[st](16) if st in icons.STATE else i + 1}</span>'
        f'<span class="nl">{e(r["label"])}</span></li>' for i, (r, st) in enumerate(zip(rows, states)))
    replay_bar = (f'<div class="replay-bar">{icons.play(16)}<span>Replay of a past run, sped up {rp.get("speed", 8):g}×. '
                  f'Nothing is running. {rp.get("elapsed", 0):.0f} of {rp.get("length", 0):.0f} s.</span></div>' if is_replay else "")
    hero = f"""<header class="hero">
  {replay_bar}
  <p class="eyebrow">{f'{e(who)} · Issue #{e(issue.get("number"))} · {e(model)}' if issue else 'New run'}</p>
  <h1>{e(issue.get('title') or ('Getting ready' if not s else 'Run ' + rid))}</h1>
  <p class="status {cls}" role="status"><span class="dot"></span><span>{e(headline(phase, cur, rows))}</span>{f'<span class="sub">· {_since(d, running_since, is_replay)}</span>' if running_since else ''}</p>
  <ol class="track" style="--n:{len(rows)}" aria-label="The {len(rows)} steps">{nodes}</ol>
</header>"""

    # ── the steps, one row each: what it gave, or what is happening in it ──
    step_rows_html = []
    for i, (r, st) in enumerate(zip(rows, states)):
        chips, cnt = inside(r["key"], d["events"])
        if st == "done":
            sub = plain.result(r["key"], s) or (chips[-1] if chips else "Done")
            trail = e(plain.duration(secs[r["key"]])) if secs.get(r["key"]) is not None else ""
        elif st == "running":
            sub = " · ".join(chips[-2:] + ([cnt] if cnt else [])) or "Starting this step"
            trail = _since(d, d.get("since"), is_replay)
        elif st == "waiting":
            sub, trail = "Read the pull request text, then approve or say no in your terminal", ""
        elif st == "stopped":
            sub, trail = plain.exit_text(outcome.get("exit")).removeprefix("Stopped. ") if outcome else "Stopped here", "Stopped"
        elif st == "skipped":
            sub, trail = "Not in this run. This step was added after it ran.", ""
        else:
            sub, trail = gives[r["key"]], ""
        ic = icons.STATE[st]() if st in icons.STATE else f'<span class="num">{i + 1}</span>'
        step_rows_html.append(_k(f"step-{i}", f'<li class="row {st}"><span class="ic">{ic}</span><span class="t"><b>{e(r["label"])}</b>'
                                 f'<span>{e(sub)}</span></span><span class="tr">{trail}</span></li>'))

    # ── latest activity ──
    label = {k: lab for k, lab, _ in plain.STEPS}
    acts = "".join(f'<li class="row"><span class="when">{e(x["at"][11:19])}</span><span class="t"><span>{e(plain.activity(x))}</span>'
                   f'<small>{e(label.get(x.get("step"), x.get("step") or ""))}</small></span></li>' for x in reversed(d["events"][-5:]))

    # ── time and money ──
    clock = s.get("fix_clock") or {}
    if clock.get("validated"):
        proven, note = plain.duration(clock["seconds"]), "Proven by 2 tests"
    elif clock.get("judges") == 1:
        proven, note = "Not proven", "Only 1 test passed"
    elif clock.get("seconds") is not None:
        proven, note = "No fix", "No fix passed the tests"
    elif outcome or phase in ("could not start", "crashed", "interrupted"):
        proven, note = "No fix", ""
    elif clock.get("started_at"):
        proven, note = _since(d, clock["started_at"], is_replay), "So far"
    else:
        proven, note = "Not started", ""
    spent, cap = m.get("spent_usd", 0) or 0, m.get("cap_usd", 0) or 0
    last_at = d["events"][-1]["at"] if d["events"] else None
    tiles = [("Time to a proven fix", proven, note),
             ("Money spent", f"${spent:.4f}" if 0 < spent < 0.1 else f"${spent:.2f}", f"Limit ${cap:.2f}" if cap else ""),
             ("Test machine time", f"{m.get('sandbox_used_s', 0) or 0} s", f"Limit {int(m.get('sandbox_cap_s') or 1800) // 60} min"),
             ("Last activity", _since(d, last_at, is_replay, " ago") if last_at else "Nothing yet", "")]
    tiles_html = "".join(_k(f"stat-{i}", f'<div class="tile"><span>{e(a)}</span><b>{b}</b>{f"<small>{e(c)}</small>" if c else ""}</div>')
                         for i, (a, b, c) in enumerate(tiles))

    # ── the next action: a glass dock, always in reach ──
    approve = f"cd ~/Projects/DebugAssist && uv run debug-assist approve {rid}"
    reject = f"cd ~/Projects/DebugAssist && uv run debug-assist reject {rid}"
    resume = f"cd ~/Projects/DebugAssist && uv run debug-assist resume {rid}"
    quiet = freshness(d).replace(" ago", "")
    if is_replay:
        title, sub, acts_html = "Replay of a past run", "Nothing is running.", (
            _link(f"?speed={rp.get('speed', 8):g}", "Start again", icons.play(16)) + _link(f"/run/{rid}", "Open the run", prominent=True))
    else:
        title, sub, acts_html = {
            "starting": ("Getting ready", "Checking that everything it needs is running.", ""),
            "could not start": ("Could not start", "Fix the items listed on this page, then start again.",
                                _link("/", "New run", icons.plus(16), True) if served else ""),
            "working": ("Nothing to do right now", "This page updates by itself.", ""),
            "interrupted": ("Interrupted", f"Nothing has happened for {quiet}. Continue it from your terminal.", _copy(resume, "Copy resume command", True)),
            "crashed": ("Crashed", "Continue it from your terminal.", _copy(resume, "Copy resume command", True)),
            "waiting": ("Your OK is needed", "Approving only saves a script. Nothing is posted to GitHub.",
                        _copy(reject, "Copy reject command") + _copy(approve, "Copy approve command", True)),
            "stopped": ("Stopped", plain.exit_text(outcome.get("exit")).removeprefix("Stopped. "),
                        _link("/", "New run", icons.plus(16), True) if served else ""),
            "done": ("Done", "The pull request text is saved. Nothing has been posted to GitHub.",
                     _copy(str(_runs_dir() / rid / "PR.md"), "Copy file path", True)),
            "idle": ("Not running", "", ""),
        }[phase]
    dock = (f'<aside class="dock glass {cls}" aria-label="What you need to do"><div class="msg"><span class="dot"></span>'
            f'<div><b>{e(title)}</b>{f"<span>{e(sub)}</span>" if sub else ""}</div></div>'
            f'{f"<div class=\"acts\">{acts_html}</div>" if acts_html else ""}</aside>')

    # ── what needs attention on the page itself ──
    why = str(outcome.get("why") or "")
    fails = [l.split("PREFLIGHT FAIL", 1)[1].strip() for l in (d.get("console") or "").splitlines() if "PREFLIGHT FAIL" in l]
    if phase == "waiting" and not is_replay:
        notice = (f'<section class="group"><h2>Approve in your terminal</h2><div class="sect">'
                  f'<div class="cmd"><code>{e(approve)}</code><button type="button" class="copy" data-copy="{e(approve)}" data-done="Copied">{icons.copy(16)}Copy</button></div>'
                  f'<div class="cmd"><code>{e(reject)}</code><button type="button" class="copy" data-copy="{e(reject)}" data-done="Copied">{icons.copy(16)}Copy</button></div>'
                  f'</div><p class="foot">The first approves the exact text below. The second says no. Neither posts anything.</p></section>')
    elif phase == "could not start":
        notice = ('<section class="group"><h2>What needs fixing</h2><div class="sect"><ul class="rows">'
                  + "".join(f'<li class="row stopped"><span class="ic">{icons.cross()}</span><span class="t"><span>{e(x)}</span></span></li>' for x in fails)
                  + "</ul></div></section>")
    elif phase in ("interrupted", "crashed") and not is_replay:
        notice = (f'<section class="group"><h2>Continue in your terminal</h2><div class="sect"><div class="cmd"><code>{e(resume)}</code>'
                  f'<button type="button" class="copy" data-copy="{e(resume)}" data-done="Copied">{icons.copy(16)}Copy</button></div></div>'
                  f'<p class="foot">It starts again after the last step that finished.</p></section>')
    elif phase == "stopped" and why:
        notice = (f'<section class="group"><div class="sect"><details class="why"><summary>Why it stopped, in technical terms</summary>'
                  f'<p>{e(why)}</p></details></div></section>')
    else:
        notice = '<section hidden></section>'

    problem = (s.get("focus") or issue.get("title") or "").strip()
    context = (f'<section class="group"><h2>The problem it is fixing</h2><div class="sect prose"><p>{e(problem[:700])}</p></div></section>'
               if problem else '<section hidden></section>')
    story = (s.get("second_story") or {}).get("text", "")
    results = '<div class="results">'
    if story:
        results += '<section class="group"><h2>Why the bug slipped through</h2><div class="sect"><div class="md" id="story"></div></div></section>'
    if d["pr_text"]:
        lock = ("" if d["pr_matches"] is not False else
                '<p class="foot" style="color:var(--red)">This file was changed after it was locked. Approval will be refused.</p>')
        results += f'<section class="group"><h2>The pull request text</h2><div class="sect"><div class="md" id="pr"></div></div>{lock}</section>'
    results += "</div>"

    read = _what_it_read(d.get("pack") or {}, s.get("context") or {})
    adv = _advisors(s)
    eng = _engineer_details(d, s, rows, live)
    md = json.dumps({"story": story, "pr": d["pr_text"]}).replace("</", "<\\/")
    css = f'<link rel="stylesheet" href="/static/app.css">' if served else f"<style>{(STATIC / 'app.css').read_text()}</style>"
    topo = ('<script src="/static/topo.js" defer></script><script src="/static/glass.js" defer></script>'
            '<script src="/static/chart.js"></script>' if served else
            "".join(f"<script>{(STATIC / f).read_text()}</script>" for f in ("topo.js", "glass.js", "chart.js")))
    nav = (f'<nav class="toolbar" aria-label="DebugAssistAgent">'
           f'<div class="tgroup glass"><a class="brand" href="{"/" if served else "#"}">{icons.mark()}<span>{plain.NAME}</span></a></div>'
           + (f'<div class="tgroup glass"><a class="tbtn" href="/#runs" aria-label="Runs">{icons.list_(16)}<span class="lbl">Runs</span></a>'
               f'<a class="tbtn" href="/" aria-label="New run">{icons.plus(16)}<span class="lbl">New run</span></a></div>'
              + (f'<div class="tgroup glass"><a class="tbtn" href="/run/{e(rid)}" aria-label="Live view">{icons.list_(16)}<span class="lbl">Live view</span></a></div>' if is_replay else
                 f'<div class="tgroup glass"><a class="tbtn" href="/replay/{e(rid)}" aria-label="Replay">{icons.play(16)}<span class="lbl">Replay</span></a></div>' if s else "")
              if served else "") + "</nav>")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
{'<meta http-equiv="refresh" content="3">' if live and not served else ''}
<title>{e(headline(phase, cur, rows))} · {plain.NAME}</title>
<meta name="color-scheme" content="dark light">
{css}
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.1.6/purify.min.js"></script>
</head><body>
<canvas id="topo" aria-hidden="true"></canvas><div class="ambient {cls}" data-kc="amb" aria-hidden="true"></div>
<div class="scrim top" aria-hidden="true"></div><div class="scrim bottom" aria-hidden="true"></div>
{nav}
<main>
{_k("hero", hero)}
{_k("dock", dock)}
{_k("notice", notice)}
<section class="group"><h2>Steps</h2><div class="sect"><ul class="rows">{"".join(step_rows_html)}</ul></div></section>
{_k("chart", f'<section class="group"><h2>How this run moved</h2>{chart.section(d)}</section>')}
{_k("read", read)}
{_k("advisors", adv)}
{_k("results", results)}
<section class="group"><h2>Latest activity</h2><div class="sect">{_k("log", f'<ul class="rows acts-list">{acts or "<li class=row><span></span><span class=t><span>Nothing yet</span></span></li>"}</ul>')}</div></section>
<section class="group"><h2>Time and money</h2><div class="sect"><div class="tiles">{tiles_html}</div></div></section>
{_k("context", context)}
{_k("final", f'<i hidden data-final="{int(final)}"></i>')}
{eng}
</main>
{_k("md", f'<script type="application/json" id="md">{md}</script>')}
{topo}
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
const fmt = s => s < 90 ? Math.round(s) + " s" : s < 5400 ? Math.round(s / 60) + " min" : (s / 3600).toFixed(1) + " h";
const tick = () => document.querySelectorAll("[data-since]").forEach(el => {{
  el.textContent = fmt(Math.max(0, (Date.now() - Date.parse(el.dataset.since)) / 1000)) + (el.dataset.suffix || "");
}});
renderMd(); tick(); setInterval(tick, 1000);
document.addEventListener("click", ev => {{
  const b = ev.target.closest("[data-copy]");
  if (!b) return;
  navigator.clipboard.writeText(b.dataset.copy).then(() => {{
    const t = b.querySelector("span") || b, was = t.textContent;
    t.textContent = b.dataset.done || "Copied"; setTimeout(() => {{ t.textContent = was; }}, 1600);
  }});
}});
{_LIVE_JS.replace("__MODE__", mode) if served else ''}
</script></body></html>"""


def _advisors(s: dict) -> str:
    """Where an advisor seat reviews a step's output, and what happened: plain words, advice only."""
    from .advisors import REVIEWS, status
    now, done, rows = status()[0], s.get("advisors") or {}, []
    said = {"OFF": "Not asked: advisors are off (not connected yet)",
            "BLOCKED": "Not asked: the advisors' server has not been reviewed yet",
            "ON": "Will be asked when this step finishes", "FAILED": "Could not be reached; the run went on without it"}
    for step, r in REVIEWS.items():
        rec = done.get(step) or {}
        st = rec.get("status", now)
        sub = (f"Said: {rec.get('answer', '')[:240]}" if st == "ANSWERED" else said.get(st, "Not asked"))
        ic = icons.check() if st == "ANSWERED" else icons.pause() if st in ("OFF", "BLOCKED") else icons.cross() if st == "FAILED" else icons.list_(18)
        rows.append(f'<li class="row {"done" if st == "ANSWERED" else "pending"}"><span class="ic">{ic}</span><span class="t">'
                    f'<b>{e(r["seat"])} reviews {e(r["what"])}</b><span>{e(sub)}</span></span></li>')
    return (f'<section class="group"><h2>Advisors</h2><div class="sect"><ul class="rows">{"".join(rows)}</ul></div>'
            '<p class="foot">Advice only. An advisor never changes the fix, the pull request text or your OK. They are '
            'switched on after the advisors\' server has been reviewed.</p></section>')


def _what_it_read(pack: dict, c: dict) -> str:
    """The gathered context, in plain words: what the later steps were given to read."""
    if not pack:
        return '<section hidden></section>'
    iss, rows = pack.get("issue") or {}, []

    def row(title: str, sub: str) -> str:
        return f'<li class="row"><span class="ic">{icons.list_(18)}</span><span class="t"><b>{e(title)}</b><span>{e(sub)}</span></span></li>'
    n = len(iss.get("comments") or [])
    rows.append(row(f"{n} comment{'s' * (n != 1)} on the issue",
                    "All of them are saved; the most useful ones go to the AI first" if n else "The issue has no comments"))
    if iss.get("linked"):
        rows.append(row(f"{len(iss['linked'])} linked issue{'s' * (len(iss['linked']) != 1)} or pull request{'s' * (len(iss['linked']) != 1)}",
                        "; ".join(f"#{x['number']} {x['title']}" for x in iss["linked"][:3])))
    for i, f in enumerate((pack.get("code") or {}).get("ranking") or []):
        name = Path(f["path"]).name
        pkg = f["path"].split("/")[1] if f["path"].startswith("packages/") else ""
        why = ", ".join(f'"{a}"' for a in f.get("matched", [])[:3]) or "words from the problem"
        rows.append(row(("Best match: " if i == 0 else "") + name, f"{pkg + ' · ' if pkg else ''}contains {why}"))
    for r in pack.get("related") or []:
        rows.append(row(f"Shared code: {r['name']}", f"From {r['module']}, called near the problem, used in {r['used_in']} files"))
    ch = sum(len(h.get("changes") or []) for h in pack.get("history") or [])
    if ch:
        last = next((h["changes"][0] for h in pack["history"] if h.get("changes")), {})
        rows.append(row(f"{ch} recent changes to the best files", f"Latest: {last.get('date', '')} {last.get('title', '')}"))
    if pack.get("missing"):
        rows.append(row("Could not read everything", "; ".join(pack["missing"])))
    foot = (f"Saved with the run and locked (fingerprint {str(c.get('sha256', ''))[:12]}). "
            "Show the bug, Find the cause and Fix it all read this, and only this." if c.get("sha256") else "")
    return (f'<section class="group"><h2>What it read</h2><div class="sect"><ul class="rows read-list">{"".join(rows)}</ul></div>'
            f'{f"<p class=foot>{e(foot)}</p>" if foot else ""}</section>')


def _engineer_details(d: dict, s: dict, rows: list[dict], live: bool) -> str:
    """Everything an engineer needs to audit the run: folded away from the operator."""
    timeline = "".join(
        f'<li class="st"><b>{i + 1}. {e(r["label"])}</b>'
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
    cases = ("".join(f"<li>✓ {e(x)}</li>" for x in of.get("passed", []))
             + "".join(f"<li>✗ {e(x)}</li>" for x in of.get("failed", []))
             + "".join(f"<li>? {e(x)}</li>" for x in of.get("broken", [])))
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
    trials = (f'<p class="note">Trials on this run (scripts that re-ran one step; never counted in the north stars):</p><ul class="cases">'
              + "".join(f"<li>{e(k)}: {v['n']} events ({e(', '.join(v['kinds']))})</li>" for k, v in sorted(d.get('trials', {}).items()))
              + '</ul>') if d.get('trials') else ''
    work = f'''<div class="grid2">
  <div class="card"><h2>Reproduce: the ladder</h2>
    <p class="note">{e(r.get('status', ''))} at rung <b>{e(r.get('rung', ''))}</b> · {confirm} · {e(r.get('attempts_used', ''))} attempt(s)</p>
    <div class="scroll"><table><tr><th>#</th><th>rung</th><th>result</th><th>test</th><th>what decided it</th></tr>{att_rows}</table></div>
    <p class="note">Judge for the fix: <code>{e(Path(r.get('oracle_test') or '').name)}</code></p></div>
  <div class="card"><h2>Cause and fix</h2>
    <p><code>{e(c.get('file', ''))}</code> lines {e('-'.join(map(str, c.get('lines', []))))}</p><p class="note">{e(c.get('why', ''))}</p>
    <div class="scroll"><table><tr><th>#</th><th>result</th><th>changed</th><th>suites</th><th>evidence</th></tr>{fix_rows}</table></div>
    <p class="note">Second test, written without seeing the fix: <b>{e(ho.get('status', 'not run'))}</b> <code>{e(Path(ho.get('test') or '').name)}</code></p>
    {f'<details><summary>Patch</summary><pre class="diff">{_diff(d["patch"])}</pre></details>' if d['patch'] else ''}</div>
</div>'''
    lessons = f'''<div class="grid2">
  <div class="card"><h2>Lasting guard</h2><p>{e(g.get('covers', ''))}</p>
    <p class="note">Laya: {e(g.get('a1_class', ''))} (p={e(g.get('a1_p', ''))}) · on the fixed code: closed {len(of.get('passed', []))} · still open {len(of.get('failed', []))} · broken {len(of.get('broken', []))}</p>
    <details><summary>Cases on the fixed code</summary><ul class="cases">{cases}</ul></details>
    <p class="note">Same code in {len(g.get('siblings', []))} other file(s), not fixed here:</p><ul class="cases">{sib}</ul>
    <p class="note">Named condition (frozen before the guard): {e((s.get('condition') or {}).get('text', ''))}</p></div>
  <div class="card"><h2>Back-test: 🎯 would have caught</h2>
    <p><b>{e(_wouldve(b))}</b>. Self-check at the anchor <code>{e((det.get('anchor') or {}).get('sha', ''))}</code>: {e(caught)} (in-sample: the guard was written from this bug)</p>
    <p class="note">On the {e(fa.get('window', '?'))} commits before: false alarms {e(fa.get('fired', '?'))} · quiet {e(fa.get('quiet', '?'))} · bug already there {e(fa.get('bug_already_there', '?'))} · unevaluable {e(fa.get('unevaluable', '?'))}</p>
    <div class="scroll"><table><tr><th>commit</th><th>date</th><th>covers</th><th>result</th><th>judge</th><th>guard</th><th>title</th></tr>{groups}</table></div></div>
</div>'''
    spend = f'''<div class="grid2">
  <div class="card"><h2>Spend, call by call</h2><div class="scroll"><table><tr><th>at</th><th>step</th><th>model</th><th>in</th><th>out</th><th>cost</th><th>status</th></tr>{calls}</table></div></div>
  <div class="card"><h2>Event log</h2><details><summary>{len(d['events'])} events</summary><div class="scroll"><table>{evs}</table></div></details>{trials}</div>
</div>'''
    return (f'<details class="eng"><summary>Details for engineers</summary><div class="eng-body">'
            f'{_k("steps", f"<div><h2>Step log</h2><ol class=tl>{timeline}</ol></div>")}{_k("work", work)}{_k("lessons", lessons)}'
            f'{_k("spend", spend)}<p class="note">Read-only. Built from this run\'s MongoDB state, event log and spend meter.</p></div></details>')


# Served pages only: ask the server for the page again and swap in the pieces whose hash changed. GET only, same
# origin; replay passes how far in it is (?ms=). Stops when the run is final; shows "viewer offline" if the server goes.
_LIVE_JS = """
const MODE = "__MODE__", T0 = Date.now();
const offline = on => document.querySelectorAll(".dock .msg span:not(.dot)").forEach(x => x.style.opacity = on ? ".4" : "");
async function poll() {
  const u = new URL(location.href);
  if (MODE === "replay") u.searchParams.set("ms", String(Date.now() - T0));
  let wait = MODE === "replay" ? 700 : 2000;
  try {
    const r = await fetch(u, { cache: "no-store" });
    if (!r.ok) throw new Error(String(r.status));
    const doc = new DOMParser().parseFromString(await r.text(), "text/html");
    let md = false, chartChanged = false;
    doc.querySelectorAll("[data-kc]").forEach(n => {
      const o = document.querySelector(`[data-kc="${n.dataset.kc}"]`);
      if (o && o.className !== n.className) o.className = n.className;
    });
    doc.querySelectorAll("[data-k]").forEach(n => {
      const o = document.querySelector(`[data-k="${n.dataset.k}"]`);
      if (o && o.dataset.h !== n.dataset.h) {
        o.replaceWith(document.importNode(n, true));
        if (["md", "results"].includes(n.dataset.k)) md = true;
        if (n.dataset.k === "chart") chartChanged = true;
      }
    });
    if (md) renderMd();
    if (chartChanged && window.DA_chart) DA_chart();
    tick();
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
