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

from . import chart, diffview, icons, plain

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
    from . import artifacts
    from .config import CFG as _cfg
    if artifacts.enabled() and not (_cfg.runs_dir / run_id / "PR.md").exists():
        artifacts.restore_run(run_id)  # cheap when nothing is missing; after a restart it brings the files back
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
    pr_patch = Path(intr["patch_path"]).read_text() if intr.get("patch_path") and Path(intr["patch_path"]).exists() else patch
    commit_msg = (Path(intr["commit_message_path"]).read_text()
                  if intr.get("commit_message_path") and Path(intr["commit_message_path"]).exists() else "")
    if intr and not commit_msg:   # paused before commit messages existed: the recommendation, worked out the same way
        try:
            from .graph import commit_message
            commit_msg = commit_message(snap.values or {})
        except Exception:
            commit_msg = ""
    rd = _cfg.runs_dir / run_id
    if not intr and at is None and (snap.values or {}).get("approval") and (rd / "PR.md").exists():
        pr_text = (rd / "PR.md").read_text()   # decided: the pull request as you approved or closed it, read-only
        pr_patch = (rd / "pr.patch").read_text() if (rd / "pr.patch").exists() else pr_patch
        commit_msg = (rd / "commit-message.txt").read_text() if (rd / "commit-message.txt").exists() else commit_msg
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
            "pr_text": pr_text, "pr_matches": pr_ok, "patch": patch, "pr_patch": pr_patch, "commit_message": commit_msg,
            "decided_by": next((x.get("by") for x in reversed(evs) if x.get("kind") == "decision" and x.get("by")), ""),
            "events": evs,
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


def _k_fmt(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


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


def _again(s: dict) -> str:
    """Run again (PM review 2026-10-08): the same issue with the same choices, started as a new run."""
    if not str(s.get("issue_url", "")).startswith("https://github.com/"):
        return ""
    h = s.get("hints") or {}
    again = {"url": s["issue_url"], "heading": s.get("focus_heading") or "", "ai": "opus" if s.get("demo") else "standard",
             "look_in": ",".join(h.get("look_in") or []), "test_in": h.get("test_in") or ""}
    return (f'<button type="button" class="btn glass prominent" data-again="{e(json.dumps(again))}">{icons.play(16)}'
            f'<span>Run again</span></button>')


def _link(href: str, label: str, icon: str = "", prominent: bool = False) -> str:
    return f'<a class="btn glass{" prominent" if prominent else ""}" href="{e(href)}">{icon}<span>{e(label)}</span></a>'


def _runs_dir() -> Path:
    from .config import CFG
    return CFG.runs_dir


def render(d: dict, mode: str = "file", replay: dict | None = None, token: str = "") -> str:
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
    proof_html = _proof(s, rid)
    pr_btn = '<button type="button" class="tbtn proof-btn" popovertarget="check-pr"><span>Check PR</span></button>'
    use = usage_by_step(d)
    ctx_html, report_html, guard_html = context_sheet(d.get("pack") or {}, s.get("context") or {}), report_sheet(s), guard_sheet(s)
    def btn(target: str, label: str) -> str:
        return f'<button type="button" class="tbtn proof-btn" popovertarget="{target}"><span>{e(label)}</span></button>'
    issue_link = (f'<a class="tbtn proof-btn" href="{e(s["issue_url"])}" target="_blank" rel="noopener noreferrer"><span>Open issue</span></a>'
                  if str(s.get("issue_url", "")).startswith("https://github.com/") else "")
    step_btns = {"read_issue": issue_link, "gather_context": btn("ctxinfo", "Context info") if ctx_html else "",
                 "reproduce": btn("proof", "Check proof") if proof_html else "",
                 "why_it_shipped": btn("report", "Read report") if report_html else "",
                 "lasting_guard": btn("guardinfo", "Guard") if guard_html else ""}
    step_rows_html = []
    for i, (r, st) in enumerate(zip(rows, states)):
        chips, cnt = inside(r["key"], d["events"])
        if st == "done":
            sub = plain.result(r["key"], s) or (chips[-1] if chips else "Done")
            trail = e(plain.duration(secs[r["key"]])) if secs.get(r["key"]) is not None else ""
            trail = step_btns.get(r["key"], "") + _tok_chip(use.get(r["key"])) + trail
            if r["key"] == "approval" and d["pr_text"]:
                trail = pr_btn + trail
        elif st == "running":
            sub = " · ".join(chips[-2:] + ([cnt] if cnt else [])) or "Starting this step"
            trail = _tok_chip(use.get(r["key"])) + _since(d, d.get("since"), is_replay)
        elif st == "waiting":
            sub = ("Check the pull request, then approve it or close it" if served and not is_replay else
                   "Check the pull request, then approve or say no in your terminal")
            trail = pr_btn if d["pr_text"] else ""
        elif st == "stopped":
            sub = plain.exit_text(outcome.get("exit")).removeprefix("Stopped. ") if outcome else "Stopped here"
            trail = step_btns.get(r["key"], "") + _tok_chip(use.get(r["key"])) + "Stopped"
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
                   f'<small>{e(label.get(x.get("step"), x.get("step") or ""))}</small></span></li>' for x in reversed(d["events"][-12:]))

    # ── usage and cost (Isha 2026-10-08: the words run dashboards use) ──
    clock = s.get("fix_clock") or {}
    if clock.get("validated"):
        proven, note = plain.duration(clock["seconds"]), "Verified by 2 tests"
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
    calls = d.get("calls") or [x for x in d["events"] if x.get("kind") == "model_call"]   # the metered calls, as Cost details
    tok_in, tok_out = (sum(int(x.get(k) or 0) for x in calls) for k in ("input_tokens", "output_tokens"))
    tiles = [("Time to fix", proven, note),
             ("LLM cost", f"${spent:.4f}" if 0 < spent < 0.1 else f"${spent:.2f}", f"Budget ${cap:.2f}" if cap else ""),
             ("Tokens", f"{_k_fmt(tok_in)} in · {_k_fmt(tok_out)} out", f"{len(calls)} model call{'s' * (len(calls) != 1)}"),
             ("Compute time", f"{m.get('sandbox_used_s', 0) or 0} s", f"Quota {int(m.get('sandbox_cap_s') or 1800) // 60} min"),
             ("Last event", _since(d, last_at, is_replay, " ago") if last_at else "None yet", "")]
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
            "waiting": ("Your OK is needed", "Check the pull request, then approve it or close it. Nothing is posted to GitHub.",
                        '<button type="button" class="btn glass prominent" popovertarget="check-pr"><span>Check PR</span></button>'),
            "stopped": ("Stopped", plain.exit_text(outcome.get("exit")).removeprefix("Stopped. "),
                        (_again(s) + _link("/", "New run", icons.plus(16))) if served else ""),
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
        notice = '<section hidden></section>'   # the decision lives in the Check PR sheet
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

    story = (s.get("second_story") or {}).get("text", "")
    adv = _advisors(s)
    eng = _engineer_details(d, s, rows, live)
    md = json.dumps({"story": story, "pr": d["pr_text"]}).replace("</", "<\\/")
    css = f'<link rel="stylesheet" href="/static/app.css">' if served else f"<style>{(STATIC / 'app.css').read_text()}</style>"
    topo = ('<script src="/static/topo.js" defer></script><script src="/static/glass.js" defer></script>' if served else
            "".join(f"<script>{(STATIC / f).read_text()}</script>" for f in ("topo.js", "glass.js")))
    badge = _k("acount", f'<span class="badge">{min(len(d["events"]), 99)}</span>')
    activity_btn = (f'<div class="tgroup glass"><button type="button" class="tbtn" popovertarget="acts" aria-label="Latest activity">'
                    f'{icons.activity(16)}<span class="lbl">Activity</span>{badge}</button></div>')
    nav = (f'<nav class="toolbar" aria-label="DebugAssistAgent">'
           f'<div class="tgroup glass"><a class="brand" href="{"/" if served else "#"}">{icons.mark()}<span>{plain.NAME}</span></a></div>'
           + activity_btn + (f'<div class="tgroup glass"><a class="tbtn" href="/#runs" aria-label="Runs">{icons.list_(16)}<span class="lbl">Runs</span></a>'
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
<section class="metrics" aria-label="Usage and cost"><div class="tiles">{tiles_html}</div>
  <button type="button" class="tbtn proof-btn" popovertarget="costs"><span>Cost details</span></button></section>
{_k("dock", dock)}
{_k("notice", notice)}
<section class="group"><h2>Steps</h2><div class="sect"><ul class="rows">{"".join(step_rows_html)}</ul></div></section>
{_k("advisors", adv)}
{_k("final", f'<i hidden data-final="{int(final)}"></i>')}
{eng}
</main>
<div id="proof" popover class="pop sheet gh" aria-label="Proof the bug happens">
  <div class="gh-top"><button type="button" class="gh-close" popovertarget="proof" popovertargetaction="hide" aria-label="Close">{icons.cross(16)}</button></div>
  <div class="gh-body">{_k("proof", proof_html or '<div class="proof gh-proof"><p class="gh-muted">Not shown yet.</p></div>')}</div>
</div>
{_check_pr(d, rid, served and not is_replay, approve, reject, waiting=phase == "waiting" and not is_replay) if d["pr_text"] else ""}
{_k("sheet-ctx", ctx_html or '<i id="ctxinfo" hidden></i>')}
{_k("sheet-report", report_html or '<i id="report" hidden></i>')}
{_k("sheet-guard", guard_html or '<i id="guardinfo" hidden></i>')}
{_k("sheet-costs", costs_sheet(d, rows))}
<div id="acts" popover class="pop glass" aria-label="Latest activity">
  <div class="pop-head"><b>Latest activity</b><button type="button" class="tbtn" popovertarget="acts" popovertargetaction="hide" aria-label="Close">{icons.cross(16)}</button></div>
  {_k("log", f'<ul class="rows acts-list">{acts or "<li class=row><span></span><span class=t><span>Nothing yet</span></span></li>"}</ul>')}
</div>
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
const renderMd = () => {{ const MD = JSON.parse(document.getElementById("md").textContent); show("story", MD.story); show("pr", MD.pr); show("pr-sheet", MD.pr); }};
// a sheet's markdown (comments, the report) is drawn when the sheet opens
const mdIn = root => root.querySelectorAll("[data-md]").forEach(el => {{
  if (el.dataset.done) return; el.dataset.done = "1"; const t = el.dataset.md;
  if (window.marked && window.DOMPurify) el.innerHTML = DOMPurify.sanitize(marked.parse(t)); else el.textContent = t;
}});
document.addEventListener("toggle", ev => {{ if (ev.newState === "open" && ev.target.classList && ev.target.classList.contains("gh")) mdIn(ev.target); }}, true);
{_DECIDE_JS.replace('__TOKEN__', json.dumps(token)) if served else ''}
const fmt = s => s < 90 ? Math.round(s) + " s" : s < 5400 ? Math.round(s / 60) + " min" : (s / 3600).toFixed(1) + " h";
const tick = () => document.querySelectorAll("[data-since]").forEach(el => {{
  el.textContent = fmt(Math.max(0, (Date.now() - Date.parse(el.dataset.since)) / 1000)) + (el.dataset.suffix || "");
}});
renderMd(); tick(); setInterval(tick, 1000);
// the page it is on, or heading to while a turn animates (quick clicks count from there)
const pagerAt = w => w._to ?? Math.round(w.querySelector(".pages").scrollLeft / Math.max(1, w.querySelector(".pages").clientWidth));
const pagerMark = (w, i) => w.querySelectorAll(".pg.num").forEach((b, k) => {{ b.classList.toggle("on", k === i); b.setAttribute("aria-selected", String(k === i)); }});
const pagerSync = w => {{ if (w._to == null) pagerMark(w, pagerAt(w)); }};
const pagerGo = (w, i) => {{ const p = w.querySelector(".pages"); i = Math.max(0, Math.min(p.children.length - 1, i));
  w._to = i; pagerMark(w, i); clearTimeout(w._t); w._t = setTimeout(() => {{ w._to = null; pagerSync(w); }}, 700);
  p.scrollTo({{ left: i * p.clientWidth, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" }}); }};
document.addEventListener("click", ev => {{   // the pull request's tabs
  const t = ev.target.closest(".gh-tabs [data-tab]"); if (!t) return;
  const sheet = t.closest(".gh");
  sheet.querySelectorAll(".gh-tabs [data-tab]").forEach(x => {{ const on = x === t; x.classList.toggle("on", on); x.setAttribute("aria-selected", String(on)); }});
  sheet.querySelectorAll(".pr-pane").forEach(p => {{ p.hidden = p.dataset.pane !== t.dataset.tab; }});
}});
document.addEventListener("click", ev => {{
  const b = ev.target.closest(".pg"); if (!b) return;
  const w = b.closest("[data-pager]"), i = pagerAt(w);
  pagerGo(w, b.classList.contains("prev") ? i - 1 : b.classList.contains("next") ? i + 1 : +b.dataset.go);
}});
document.addEventListener("scroll", ev => {{ const w = ev.target.closest && ev.target.closest("[data-pager]"); if (w) pagerSync(w); }}, true);
document.addEventListener("keydown", ev => {{
  const p = ev.target.closest && ev.target.closest(".pages"); if (!p || !["ArrowLeft", "ArrowRight"].includes(ev.key)) return;
  ev.preventDefault(); const w = p.closest("[data-pager]"); pagerGo(w, pagerAt(w) + (ev.key === "ArrowRight" ? 1 : -1));
}});
document.addEventListener("toggle", ev => {{
  if (ev.target.id !== "proof" || ev.newState !== "open") return;
  ev.target.querySelectorAll("[data-pager]").forEach(w => {{ const p = w.querySelector(".pages"); p.scrollLeft = (+p.dataset.start || 0) * p.clientWidth; pagerSync(w); }});
}}, true);
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


LEVELS = [("unit", "Unit test"), ("integration", "Integration test"), ("end_to_end", "Automation test (end to end)")]
NOT_TRIED = {"integration": "This repo has no recorded real data to build one from.",
             "end_to_end": "It would need live API keys and the internet. The test machine has neither, on purpose."}


def _pill(text: str, tone: str) -> str:
    return f'<span class="pill {tone}">{e(text)}</span>'


def _try_outcome(a: dict) -> tuple[str, str]:
    """(plain words, tone) for one try."""
    ev = a.get("evidence") or ""
    if a.get("outcome") == "RED":
        return "Shows the bug", "red"
    if a.get("outcome") == "GREEN":
        return "Passed: did not show the bug", "grey"
    if ev.startswith("writer refused"):
        return "Draft refused before it ran", "amber"
    if ev.startswith("RED for another reason"):
        return "Failed, but for another reason", "amber"
    return "Did not run properly", "amber"


GH_STATUS = {"pass": ("Successful", "ok"), "fail": ("Failing", "bad"), "skip": ("Skipped", "muted"),
             "wait": ("Expected: waiting for the fix", "warn"), "none": ("Not re-run", "muted")}


def checks_of(s: dict) -> list[dict]:
    """The checks a reviewer would see on the pull request, each on `main` and with the change (Isha 2026-10-08: the
    proof as GitHub shows checks). From the run's own records: the repo's tests, each test written, the second test."""
    r, f = s.get("repro") or {}, s.get("fix") or {}
    after = {x["test_path"]: x.get("outcome") for x in f.get("after_fix") or []}
    validated = f.get("status") == "VALIDATED"
    out, ex = [], r.get("existing_tests") or {}
    if ex:
        n = (ex.get("passed") or 0) + (ex.get("failed") or 0)
        out.append({"name": f"Existing tests / {ex.get('package', '')}", "main": "pass" if ex.get("status") == "NONE FAIL" else
                    "fail" if ex.get("status") in ("FOUND", "OTHER FAILURES") else "none",
                    "change": "pass" if validated else "wait",
                    "detail": f"{n} tests" + (f", {ex.get('failed')} failing" if ex.get("failed") else "")})
    judge = r.get("oracle_test") or r.get("failing_test")
    for path in dict.fromkeys(x for x in (r.get("failing_test"), r.get("oracle_test")) if x):
        kind = diffview.kind_of_test(path)
        got = after.get(path)
        change = ("pass" if got == "GREEN" else "fail") if got else ("pass" if validated and path == judge else "wait" if not validated else "none")
        out.append({"name": f"{kind.capitalize()} test / {Path(path).name}", "main": "fail", "change": change,
                    "detail": "written for this issue: fails on main, showing the problem"})
    ho = f.get("holdout") or {}
    if ho.get("test"):
        out.append({"name": f"Second test / {Path(ho['test']).name}", "main": "fail",
                    "change": "pass" if str(ho.get("status", "")).startswith("PASSED") else "fail",
                    "detail": "written without seeing the change"})
    skipped = (r.get("ladder_plan") or {}).get("skipped") or {}
    if "end_to_end" in skipped:
        out.append({"name": "Automation test (end to end)", "main": "skip", "change": "skip",
                    "detail": "needs live provider keys and the internet; the test machine has neither, on purpose"})
    return out


def _gh_checks(rows: list[dict]) -> str:
    if not rows:
        return '<p class="gh-muted">No checks yet.</p>'
    def st(k, col):
        word, tone = GH_STATUS[k]
        return f'<span class="gh-st {tone}" data-col="{col}"><span class="gh-dot"></span>{e(word)}</span>'
    return ('<div class="gh-box cols"><div class="gh-box-head"><span>Check</span><span>On main</span><span>With this change</span></div>'
            + "".join(f'<div class="gh-check"><span class="gh-cname"><b>{e(c["name"])}</b><span class="gh-muted">{e(c["detail"])}</span></span>'
                      f'{st(c["main"], "On main")}{st(c["change"], "With this change")}</div>' for c in rows) + "</div>")


_LOG_BAD = re.compile(r"AssertionError|\w*Error\b|\bFAIL\b|\bfailed\b|×|✗|^\s*not ok|^\s*E\s{2,}")


def short_cmd(ran: str) -> tuple[str, str]:
    """(the command that runs the tests, the setup before it): "cd packages/ai && pnpm test:node", "export …"."""
    if ran.startswith("cd ") or " && " not in ran:   # already the short form
        return ran, ""
    short = ("cd " + ran.split(" && cd ", 1)[1]) if " && cd " in ran else ran.rsplit(" && ", 1)[-1]
    return short, ran[: len(ran) - len(short)].rstrip(" &") if short != ran else ""


def _actions_log(cmd: str, output: str, opened: bool = True) -> str:
    """A test run as GitHub Actions shows a step: `Run <command>`, then the log with numbered lines, errors red. The
    setup before the command is folded under the title (Isha 2026-10-09: the long command made the proof hard to read)."""
    cmd, setup = short_cmd(cmd)
    all_lines = (output or "").splitlines()
    lines, skip = all_lines[-400:], max(0, len(all_lines) - 400)
    rows = "".join(f'<tr class="{"err" if _LOG_BAD.search(l) else ""}"><td class="ln">{i}</td><td>{e(l)}</td></tr>'
                   for i, l in enumerate(lines, skip + 1))
    more = f'<p class="gh-muted gh-cut">The first {skip} lines are in the proof file.</p>' if skip else ""
    fold = (f'<details class="ran-full"><summary>Setup</summary><code>{e(setup)}</code></details>' if setup else "")
    return (f'<details class="gh-step"{" open" if opened else ""}><summary>Run {e(cmd)}</summary>'
            f'<div class="gh-log">{fold}{more}<table>{rows}</table></div></details>')


def _check_pr(d: dict, rid: str, can_decide: bool, approve_cmd: str, reject_cmd: str, waiting: bool = True) -> str:
    """The pull request as GitHub shows it, in GitHub's words and colours (Isha 2026-10-08): title, Draft, "wants to
    merge 1 commit into main from …", then Conversation, Commits (the commit message and extended description, yours to
    edit), Checks (the tests, on main and with the change) and Files changed (git style); at the bottom, Approve or Close
    pull request. Approving binds to exactly this text and change (its sha256); either runs the terminal's own command."""
    sha = str((d.get("interrupt") or {}).get("sha256") or "")
    st = d.get("state") or {}
    issue = st.get("issue") or {}
    msg = (d.get("commit_message") or "").rstrip("\n")
    title, _, extended = msg.partition("\n")
    title = title or f"fix: #{issue.get('number', '')}"
    files = diffview.parse(d.get("pr_patch") or "")
    add, rem = sum(f["added"] for f in files), sum(f["removed"] for f in files)
    rows = checks_of(st)
    got = [c["change"] for c in rows]
    n = {k: got.count(k) for k in GH_STATUS}
    counts = ", ".join(x for x in (f"{n['fail']} failing" * bool(n["fail"]), f"{n['wait']} expected" * bool(n["wait"]),
                                   f"{n['pass']} successful" * bool(n["pass"]), f"{n['skip']} skipped" * bool(n["skip"]),
                                   f"{n['none']} not re-run with the change" * bool(n["none"])) if x)
    verdict = ("Some checks were not successful" if n["fail"] else "Some checks haven't completed yet" if n["wait"] else
               "All checks have passed" if n["pass"] else "No checks ran")   # GitHub's merge box, in its words
    head = f'debugassist/fix-{issue.get("number", "")}'
    said = (st.get("approval") or {}).get("status", "")
    badge = ('<span class="gh-state draft">Draft</span>' if waiting or not said else
             '<span class="gh-state closed">Closed</span>' if said == "REJECTED" else '<span class="gh-state open">Approved</span>')
    lock = ('<div class="gh-annot bad"><b>This text was changed after it was locked. Approval will be refused.</b></div>'
            if d.get("pr_matches") is False and waiting else "")
    who_did = e(d.get("decided_by") or "You")
    done = (f'<div class="gh-merge"><div class="gh-merge-lines"><b>{who_did} {"closed" if said == "REJECTED" else "approved"} this pull request</b>'
            f'<span class="gh-muted">{e(counts)}</span><span class="gh-muted">'
            + ("Nothing was posted to GitHub." if said == "REJECTED" else
               "Nothing was posted to GitHub. publish.sh in the run's folder lists the git steps for you to run.")
            + '</span></div></div>')
    decide = done if not waiting else (f'<div class="decide gh-merge" data-run="{e(rid)}" data-sha="{e(sha)}">'
              f'<div class="gh-merge-lines"><b>{verdict}</b><span class="gh-muted">{e(counts)}</span>'
              f'<span class="gh-muted">This pull request is a draft. Approving records your approval for exactly this text and '
              f'change; nothing is posted to GitHub.</span></div>'
              f'<div class="gh-merge-btns"><button type="button" class="gh-btn danger" data-decide="reject" data-label="Close pull request" '
              f'data-confirm="Confirm: close pull request"><span>Close pull request</span></button>'
              f'<button type="button" class="gh-btn primary" data-decide="approve" data-label="Approve" '
              f'data-confirm="Confirm approve"><span>Approve</span></button></div>'
              f'<p class="decide-msg" role="status"></p></div>') if can_decide else (
              f'<div class="gh-box"><div class="cmd"><code>{e(approve_cmd)}</code></div><div class="cmd"><code>{e(reject_cmd)}</code></div></div>')
    commits = ((f'<div class="gh-box gh-commit"><label class="gh-label" for="commit-title">Commit message</label>'
                f'<input id="commit-title" class="gh-input" value="{e(title)}" data-recommended="{e(title)}" autocomplete="off">'
                f'<label class="gh-label" for="commit-body">Extended description</label>'
                f'<textarea id="commit-body" class="gh-input mono" rows="8" data-recommended="{e(extended.strip())}">{e(extended.strip())}</textarea>'
                f'<p class="gh-muted"><span id="commit-note">Recommended by DebugAssistAgent. Edit it: your message is what the '
                f'commit carries, saved with your approval.</span> <button type="button" class="gh-link" id="commit-reset">'
                f'Restore the recommendation</button></p></div>') if can_decide else
               f'<div class="gh-box"><pre class="gh-pre">{e(msg)}</pre></div>')
    who = "debugassist"
    return (f'<div id="check-pr" popover class="pop sheet gh" aria-label="Pull request">'
            f'<div class="gh-top"><button type="button" class="gh-close" popovertarget="check-pr" popovertargetaction="hide" '
            f'aria-label="Close">{icons.cross(16)}</button>'
            f'<h3 class="gh-title"><span id="pr-title">{e(title)}</span> <span class="gh-muted">#{e(issue.get("number", ""))}</span></h3>'
            f'<p class="gh-meta">{badge}<b>{who}</b> wants to merge 1 commit into '
            f'<code class="gh-ref">main</code> from <code class="gh-ref">{e(head)}</code>'
            f'<span class="gh-diffstat"><span class="plus">+{add}</span> <span class="minus">−{rem}</span></span></p>'
            f'<nav class="gh-tabs" role="tablist">'
            f'<button type="button" role="tab" class="on" data-tab="conv" aria-selected="true">Conversation</button>'
            f'<button type="button" role="tab" data-tab="commits" aria-selected="false">Commits <span class="cnt">1</span></button>'
            f'<button type="button" role="tab" data-tab="checks" aria-selected="false">Checks <span class="cnt">{len(rows)}</span></button>'
            f'<button type="button" role="tab" data-tab="files" aria-selected="false">Files changed <span class="cnt">{len(files)}</span></button></nav></div>'
            f'<div class="prv gh-body">'
            f'<div class="pr-pane" data-pane="conv">{lock}<div class="gh-comment"><div class="gh-comment-head"><b>{who}</b> opened this '
            f'pull request</div><div class="md gh-md" id="pr-sheet"></div></div></div>'
            f'<div class="pr-pane" data-pane="commits" hidden>{commits}</div>'
            f'<div class="pr-pane" data-pane="checks" hidden>{_gh_checks(rows)}</div>'
            f'<div class="pr-pane" data-pane="files" hidden>{diffview.html(d.get("pr_patch") or "")}</div>'
            f'{decide}' + (f'<p class="gh-muted gh-fp">Fingerprint sha256 <code>{e(sha[:12])}</code></p>' if sha else "")
            + '</div></div>')


def _gh_row(dot: str, name: str, word: str, detail: str, extra: str = "") -> str:
    return (f'<div class="gh-check"><span class="gh-dot {dot}"></span><span class="gh-cname"><b>{e(name)}</b>'
            f'<span class="gh-muted">{e(detail)}</span>{extra}</span><span class="gh-st {dot}">{e(word)}</span></div>')


def _proof(s: dict, rid: str) -> str:
    """Proof the bug happens, as GitHub shows a workflow run (Isha 2026-10-08: "check proof should also have git style
    format", in GitHub's words and colours): a Summary of checks (a test already in the repo failing for this issue?
    then unit, integration, automation), then every try as a job, turned to by number: its annotation (the failing
    assertion), the test as a change to the code (git style), and its steps and log (as Actions shows them).
    Empty until the bug has been shown."""
    from .testwriter import read_proof
    r = s.get("repro") or {}
    if r.get("status") != "REPRODUCED" or not r.get("failing_test"):
        return ""
    rdir = _runs_dir() / rid
    tries = sorted((a for a in s.get("attempts") or [] if a.get("step", "reproduce") == "reproduce" and a.get("n")),
                   key=lambda a: a["n"])
    fix = s.get("fix") or {}
    after = {x["test_path"]: x for x in fix.get("after_fix") or []}
    judge = r.get("oracle_test") or r.get("failing_test")
    issue = s.get("issue") or {}
    prof = s.get("profile") or {}

    def after_fix(path: str) -> tuple[str, str]:
        """(GitHub status word, tone) for the test once the change is in."""
        got = after.get(path)
        if got:
            return ("Successful with the change", "ok") if got["outcome"] == "GREEN" else ("Still failing with the change", "bad")
        if not fix.get("status"):
            return "Expected: waiting for the fix", "warn"
        if fix["status"] != "VALIDATED":
            return "No change passed yet", "muted"
        if path == judge:  # a validated fix passed its judging test by definition
            return "Successful with the change", "ok"
        return "Not re-run with the change (runs before 8 Oct 2026)", "muted"

    # 1. the summary: one check per question
    rows = []
    ex = r.get("existing_tests") or {}
    pkg = ex.get("package") or ""
    total = (ex.get("passed") or 0) + (ex.get("failed") or 0)
    if ex.get("status") == "FOUND":
        ans, dot, why = "Found", "bad", f"Already in the repo and failing on main for this issue: {', '.join(ex['for_issue'][:2])}"
    elif ex.get("status") == "NONE FAIL":
        ans, dot, why = "Not found", "muted", (f"All {total} of the repo's own tests in {pkg} pass on main" if total else
                                               f"The repo's own tests in {pkg} all pass on main") + ", so none catches it. A test was written for it."
    elif ex.get("status") == "OTHER FAILURES":
        ans, dot, why = "Not found", "muted", (f"{ex.get('failed', 'Some')} of the repo's own tests in {pkg} fail on main, but "
                                               "none for this issue's reason. A test was written for it.")
    else:
        ans, dot, why = "Not checked", "muted", ex.get("why") or "Runs before 8 Oct 2026 did not check the repo's own tests first."
    epf = read_proof(rdir / "proof" / "existing-tests.txt")
    elog = f'<div class="gh-steps">{_actions_log(epf["ran"], epf.get("output", ""), opened=False)}</div>' if epf.get("ran") else ""
    rows.append(_gh_row(dot, "Test already in the repo failing for this issue?", ans, why, elog))
    skipped = (r.get("ladder_plan") or {}).get("skipped") or {}
    for key, label in LEVELS:
        at = [a for a in tries if a.get("rung") == key]
        red = next((a for a in at if a.get("outcome") == "RED"), None)
        extra = ""
        if red:
            word, tone = after_fix(red.get("test_path", ""))
            ans, dot = "Found", "bad"
            why = (f"Written for this issue (try {red['n']}): fails on main, showing the problem"
                   + (", on real recorded data." if key == "integration" else "."))
            extra = f'<span class="gh-after gh-st {tone}"><span class="gh-dot"></span>{e(word)}</span>'
        elif at:
            ans, dot = "Not found", "muted"
            why = f"Tried {len(at)} time{'s' * (len(at) != 1)}: " + "; ".join(_try_outcome(x)[0].lower() for x in at) + "."
        elif key in skipped:
            beside = re.match(r"no recorded data beside (.+)", skipped[key])
            why = (f"There is no recorded real data beside the code at fault ({beside.group(1)}), so there is nothing to "
                   "build one from." if beside else NOT_TRIED.get(key, "Not available for this issue."))
            ans, dot = ("Not possible here" if beside else "Skipped"), "muted"
        else:
            ans, dot, why = "Not needed", "muted", "The bug was already shown and confirmed."
        rows.append(_gh_row(dot, f"{label} failing for this issue?", ans, why, extra))
    found = [x for x in tries if x.get("outcome") == "RED"]
    first = found[0] if found else None
    gist = ""
    if first:
        kind = plain.TEST_KIND.get(first.get("rung", ""), "test")
        aw, _ = after_fix(first.get("test_path", ""))
        before = ("The repo's own tests already include one that fails for this issue. " if ex.get("status") == "FOUND" else
                  f"All {total:,} of the repo's own tests pass on main, so none of them catches this bug. " if ex.get("status") == "NONE FAIL" and total else "")
        gist = (f'<div class="gh-annot muted"><b>In short</b><p>{e(before)}A new {e(kind)} written for this issue (try {first["n"]}) '
                f'fails on main for the reason the issue describes. With the fix: {e(aw[0].lower() + aw[1:])}.</p></div>')
    summary = (f'{gist}<details class="gh-box gh-box-fold" open><summary class="gh-box-head"><span>Checks</span>'
               f'<span class="gh-muted">{len(rows)} questions</span></summary>{"".join(rows)}</details>')

    # 2. every try, one job each
    pages, nums, start = [], [], None   # opens on the first try that showed the bug (try 1 counts: index 0)
    for i, a in enumerate(tries):
        path, name = a.get("test_path") or "", Path(a.get("test_path") or "").name
        word, tone = _try_outcome(a)
        dot = {"red": "bad", "grey": "ok", "amber": "warn"}.get(tone, "muted")
        word = {"Shows the bug": "Failing on main: shows the bug", "Passed: did not show the bug": "Passed: did not show the bug"}.get(word, word)
        if a.get("outcome") == "RED" and start is None:
            start = i
        kind = plain.TEST_KIND.get(a.get("rung", ""), "test")
        pf = {}
        if name:
            pf = read_proof(rdir / "proof" / f"try-{a['n']}-{name}.txt") or read_proof(rdir / "proof" / f"{name}.txt")
        ev = a.get("evidence") or ""
        lines = [l for l in ev.splitlines() if not l.startswith("writer's symptom:")]
        checks = next((l.removeprefix("writer's symptom:").strip() for l in ev.splitlines() if l.startswith("writer's symptom:")), "")
        head = (f'<div class="gh-job-head"><span class="gh-dot {dot}"></span><h3>Try {a["n"]} · {e(kind)}</h3>'
                f'<span class="gh-st {dot}">{e(word)}</span></div>')
        num = (f'<button type="button" class="pg num {tone}{" on" if i == 0 else ""}" role="tab" data-go="{i}" '
               f'aria-label="Try {a["n"]}: {e(word)}">{a["n"]}</button>')
        if ev.startswith("writer refused"):   # never ran: say so, and why (#22288 run: refused drafts read as if they had run)
            pages.append(f'<article class="page gh-job" aria-label="Try {a["n"]}">{head}'
                         f'<div class="gh-annot warn"><b>Not run: the draft was refused before it ran</b>'
                         f'<pre>{e(ev.removeprefix("writer refused:").strip()[:1000])}</pre></div>'
                         f'<p class="gh-muted">A draft that breaks a rule the code enforces is refused and never run; it still '
                         f'counts toward the 4 tries.</p></article>')
            nums.append(num)
            continue
        code = ""
        if not pf.get("diff"):
            code_file = next((f for f in (rdir / "checkout" / path, rdir / "attempt-tests" / name) if path and f.exists()), None)
            code = code_file.read_text(errors="replace") if code_file else ""
        diff = pf.get("diff") or (diffview.new_file_diff(path, code) if code else "")
        annot = ""
        if lines:
            annot = (f'<div class="gh-annot {"bad" if a.get("outcome") == "RED" else "warn" if dot == "warn" else "muted"}">'
                     f'<b>{e(lines[0][:300])}</b>'
                     + (f'<pre>{e(chr(10).join(lines[1:]).strip()[:2000])}</pre>' if len(lines) > 1 else "")
                     + (f'<p><span class="gh-muted">What the test checks:</span> {e(checks)}</p>' if checks else "") + "</div>")
        after_html = ""
        if a.get("outcome") == "RED":
            aw, at_ = after_fix(path)
            after_html = f'<div class="gh-annot {at_}"><b>{e(aw)}</b></div>'
        ran = pf.get("ran", "")
        short, setup = short_cmd(ran) if ran else ("", "")
        steps = ""
        if ran:
            setup_lines = [f"Runner: {pf.get('where', '')}", f"Code: {pf.get('code', '')}", f"Started: {pf.get('when', '')}"]
            if setup:
                setup_lines += ["", *[f"$ {x.strip()}" for x in setup.split(" && ")]]
            steps = (f'<div class="gh-steps">'
                     f'<details class="gh-step"><summary>Set up job</summary><div class="gh-log"><table>'
                     + "".join(f'<tr><td class="ln">{k}</td><td>{e(l)}</td></tr>' for k, l in enumerate(setup_lines, 1))
                     + '</table></div></details>'
                     + _actions_log(short, pf.get("output", ""))
                     + f'<details class="gh-step"><summary>Complete job</summary><div class="gh-log"><table>'
                     f'<tr><td class="ln">1</td><td>{e(f"Process completed with exit code {pf.get("exit_code", "")}.")}</td></tr>'
                     f'<tr><td class="ln">2</td><td>{e(f"Test fingerprint sha256 {pf.get("sha256", "")}")}</td></tr>'
                     f'</table></div></details></div>')
        else:
            steps = '<p class="gh-muted">The exact command and full output were not kept for runs before 8 Oct 2026.</p>'
        pages.append(f'<article class="page gh-job" aria-label="Try {a["n"]}">{head}{annot}{after_html}'
                     + (f'<h4 class="gh-h">The test</h4>{diffview.html(diff, open_files=1)}' if diff else "")
                     + f'<h4 class="gh-h">Steps</h4>{steps}</article>')
        nums.append(num)
    pager = (f'<div class="pager-wrap" data-pager><div class="pager-bar"><h4 class="gh-h">Jobs</h4><div class="pager" role="tablist" aria-label="Tries">'
             f'<button type="button" class="pg prev" aria-label="Previous try">{icons.chevron(14)}</button>{"".join(nums)}'
             f'<button type="button" class="pg next" aria-label="Next try">{icons.chevron(14)}</button></div></div>'
             f'<div class="pages" tabindex="0" data-start="{start or 0}">{"".join(pages)}</div></div>') if pages else ""
    shown = next((a["n"] for a in tries if a.get("outcome") == "RED"), None)
    base = str(prof.get("base_commit") or "")[:7]
    title = (f'<h3 class="gh-title">Reproduce issue <span class="gh-muted">#{e(issue.get("number", ""))}</span></h3>'
             f'<p class="gh-meta"><span class="gh-state failure">Bug shown</span> on <code class="gh-ref">main</code>'
             + (f' at <code class="gh-ref">{e(base)}</code>' if base else "")
             + (f' · try {shown} of {len(tries)}' if shown else "") + ' · run by DebugAssistAgent, internet off</p>')
    return (f'<div class="proof gh-proof">{title}<h4 class="gh-h">Summary</h4>{summary}{pager}<p class="gh-muted gh-fp">The code '
            'was the repository as it is, with only the test added. A test that fails for a different reason does not count '
            'as showing the bug.</p></div>')


# ── sheets that open from the steps (Isha 2026-10-09): Context info, Read report, Guard, Cost details ─────────────
def usage_by_step(d: dict) -> dict:
    """{step: {calls, in, out, usd}} from the run's metered AI calls."""
    out = {}
    for c in d.get("calls") or []:
        u = out.setdefault(c.get("step") or "", {"calls": 0, "in": 0, "out": 0, "usd": 0.0})
        u["calls"] += 1
        u["in"] += int(c.get("input_tokens") or 0)
        u["out"] += int(c.get("output_tokens") or 0)
        u["usd"] += (c.get("actual_micro") or 0) / 1e6
    return out


def _tok_chip(u: dict | None) -> str:
    if not u or not u["calls"]:
        return ""
    return (f'<span class="tokchip" title="{u["calls"]} AI call{"s" * (u["calls"] != 1)}: {u["in"]:,} tokens in, '
            f'{u["out"]:,} out">{_k_fmt(u["in"] + u["out"])} tokens · ${u["usd"]:.2f}</span>')


def _sheet(sid: str, label: str, title: str, meta: str, body: str, tabs: list | None = None) -> str:
    """A GitHub-style sheet: title, one meta line, optional underline tabs, then the body (panes when tabs)."""
    nav = ""
    if tabs:
        nav = ('<nav class="gh-tabs" role="tablist">' + "".join(
            f'<button type="button" role="tab" data-tab="{k}" class="{"on" if i == 0 else ""}" aria-selected="{str(i == 0).lower()}">'
            f'{e(name)}{f" <span class=cnt>{n}</span>" if n is not None else ""}</button>' for i, (k, name, n) in enumerate(tabs))
            + '</nav>')
    return (f'<div id="{sid}" popover class="pop sheet gh" aria-label="{e(label)}"><div class="gh-top">'
            f'<button type="button" class="gh-close" popovertarget="{sid}" popovertargetaction="hide" aria-label="Close">{icons.cross(16)}</button>'
            f'<h3 class="gh-title">{title}</h3><p class="gh-meta">{meta}</p>{nav}</div><div class="gh-body">{body}</div></div>')


def _md(text: str) -> str:
    """Markdown rendered in the browser (marked + DOMPurify) when its sheet opens."""
    return f'<div class="md gh-md" data-md="{e(text or "")}"></div>'


def _blob(snippets: str, marks: list) -> str:
    """Numbered source lines (from the context pack) as GitHub shows a file, the lines with the issue's strings marked."""
    rows = []
    for line in (snippets or "").splitlines():
        m = re.match(r"^\s*(\d+)  (.*)$", line)
        if not m:
            rows.append('<tr class="gap"><td class="ln"></td><td class="code">…</td></tr>')
            continue
        hit = any(a and a in m.group(2) for a in marks)
        rows.append(f'<tr class="{"hit" if hit else ""}"><td class="ln">{m.group(1)}</td><td class="code">{e(m.group(2))}</td></tr>')
    return f'<div class="dscroll"><table class="dtable blob">{"".join(rows)}</table></div>'


def context_sheet(pack: dict, c: dict) -> str:
    """Everything Gather context read, GitHub's way (Isha 2026-10-09): the comments as comments, the history as a
    commit list, the code as files with the issue's strings marked, the shared code. Collected by code, not AI."""
    if not pack:
        return ""
    iss, code = pack.get("issue") or {}, pack.get("code") or {}
    ctx = code.get("ctx") or {}
    cs, links = iss.get("comments") or [], iss.get("linked") or []
    conv = "".join(f'<div class="gh-comment"><div class="gh-comment-head"><b>Comment {i}</b> · commented on {e(str(x.get("at") or "")[:10])}</div>'
                   f'{_md(x.get("text", ""))}</div>' for i, x in enumerate(cs, 1)) or '<p class="gh-muted">The issue has no comments.</p>'
    if links:
        conv += ('<div class="gh-box"><div class="gh-box-head"><span>Linked issues and pull requests</span></div>'
                 + "".join(f'<div class="gh-check"><span class="gh-dot {"ok" if x.get("merged") else "muted"}"></span>'
                           f'<span class="gh-cname"><b>{e(x.get("title", ""))}</b><span class="gh-muted">{e(x.get("repo", ""))}#{e(x.get("number", ""))} · '
                           f'{"pull request" if x.get("pull_request") else "issue"} · {e(x.get("state", ""))}{" · merged" if x.get("merged") else ""}</span></span></div>'
                           for x in links) + '</div>')
    errs = (iss.get("errors") or {})
    if errs.get("errors") or errs.get("frames"):
        conv += f'<div class="gh-box"><div class="gh-box-head"><span>Errors quoted in the issue</span></div><pre class="gh-pre">{e(chr(10).join(errs.get("errors", []) + ["at " + f for f in errs.get("frames", [])]))}</pre></div>'
    hist = pack.get("history") or []
    commits = "".join(
        f'<div class="gh-box"><div class="gh-box-head"><span>Commits on <code>{e(h["path"])}</code></span></div>'
        + ("".join(f'<div class="gh-commit-row"><span class="gh-cname"><b>{e(x.get("title", ""))}</b>'
                   f'<span class="gh-muted">committed on {e(x.get("date", ""))}</span></span><code class="gh-sha">{e(str(x.get("sha", ""))[:7])}</code></div>'
                   for x in h.get("changes") or []) or '<p class="gh-muted gh-pad">No recent changes found.</p>') + '</div>'
        for h in hist) or '<p class="gh-muted">No history was read.</p>'
    ranking = code.get("ranking") or []
    files = ""
    for i, f in enumerate(ranking):
        why = ", ".join(f"“{a}”" for a in (f.get("matched") or [])[:4]) or "the problem's words"
        body = _blob(ctx.get("snippets", ""), f.get("matched") or []) if f["path"] == ctx.get("source") and ctx.get("snippets") else ""
        files += (f'<details class="dfile"{" open" if i == 0 else ""}><summary><span class="dpath">{e(f["path"])}</span>'
                  f'<span class="dtag">{"best match · " if i == 0 else ""}contains {e(why)}</span></summary>{body}</details>')
    if ctx.get("look_in") or ctx.get("look_in_missing") or ctx.get("test_into") or ctx.get("test_into_note"):
        files = (f'<div class="gh-annot muted"><b>Your directional input</b>'
                 + (f'<p>Looked in first: {e(", ".join(ctx.get("look_in") or []))}</p>' if ctx.get("look_in") else "")
                 + (f'<p>Not found in the code: {e(", ".join(ctx["look_in_missing"]))}</p>' if ctx.get("look_in_missing") else "")
                 + (f'<p>Unit test written in: {e(ctx["test_into"])}</p>' if ctx.get("test_into") else "")
                 + (f'<p>Test file not used: {e(ctx["test_into_note"])}</p>' if ctx.get("test_into_note") else "") + '</div>') + files
    files = files or '<p class="gh-muted">No files matched.</p>'
    rel = pack.get("related") or []
    shared = "".join(f'<details class="dfile"><summary><span class="dpath">{e(r["name"])}</span><span class="dtag">from {e(r.get("module", ""))} · '
                     f'used in {e(r.get("used_in", 0))} files</span></summary><pre class="gh-pre">{e(str(r.get("definition", ""))[:6000])}</pre></details>'
                     for r in rel) or '<p class="gh-muted">No shared code was called near the problem.</p>'
    n = pack.get("counts") or {}
    def many(k, one, more):
        return f"{k} {one if k == 1 else more}"
    meta = (f'<span class="gh-state neutral">Collected by code, no AI</span>Read {many(n.get("comments", len(cs)), "comment", "comments")}, '
            f'{many(n.get("files", len(ranking)), "file", "files")}, {many(n.get("related", len(rel)), "piece", "pieces")} of shared code and '
            f'{many(n.get("changes", 0), "recent change", "recent changes")} · locked, fingerprint <code>{e(str(c.get("sha256", ""))[:12])}</code>')
    panes = (f'<div class="pr-pane" data-pane="conv">{conv}</div><div class="pr-pane" data-pane="commits" hidden>{commits}</div>'
             f'<div class="pr-pane" data-pane="code" hidden>{files}</div><div class="pr-pane" data-pane="shared" hidden>{shared}</div>'
             f'<p class="gh-muted gh-fp">Show the bug, Find the cause and Fix it read this, and only this.</p>')
    return _sheet("ctxinfo", "Context info", f'Context for issue <span class="gh-muted">#{e(iss.get("number", ""))}</span>', meta, panes,
                  [("conv", "Conversation", len(cs)), ("commits", "Commits", sum(len(h.get("changes") or []) for h in hist)),
                   ("code", "Code", len(ranking)), ("shared", "Shared code", len(rel))])


def report_sections(text: str) -> tuple[list, str]:
    """The report's questions and answers ("### question" headings), and its CONDITION line."""
    cond = ""
    m = re.search(r"^CONDITION:\s*(.+)$", text or "", re.M)
    if m:
        cond = m.group(1).strip()
        text = text[:m.start()]
    parts = re.split(r"^###\s+(.+?)\s*$", text or "", flags=re.M)
    out = [(parts[i].strip(), parts[i + 1].strip()) for i in range(1, len(parts) - 1, 2)]
    return out, cond


def report_sheet(s: dict) -> str:
    """Why the bug slipped through, as a GitHub-style report (Isha 2026-10-09: collapsible, GitHub-intuitive): the
    short answer first, then each question folded."""
    text = (s.get("second_story") or {}).get("text", "")
    if not text or text.startswith("PLACEHOLDER"):
        return ""
    qa, cond = report_sections(text)
    if not qa:
        body = _md(text)
    else:
        first = qa[0][1]
        body = (f'<div class="gh-annot muted"><b>In short</b>{_md(first)}'
                + (f'<p><span class="gh-muted">The condition that let it ship:</span> {e(cond)}</p>' if cond else "") + '</div>'
                + "".join(f'<details class="gh-fold"{" open" if i == 0 else ""}><summary>{e(q)}</summary>{_md(a)}</details>'
                          for i, (q, a) in enumerate(qa)))
    issue = s.get("issue") or {}
    meta = ('<span class="gh-state ai">Written by AI</span>from the run\'s evidence: the history of the code at fault, its '
            'review and release, and the issue · conditions, never people')
    return _sheet("report", "Why the bug slipped through", f'Why the bug slipped through <span class="gh-muted">#{e(issue.get("number", ""))}</span>',
                  meta, body)


def guard_sheet(s: dict) -> str:
    """Guard similar bugs, in plain words (Isha 2026-10-09: "I don't know exactly what happens here")."""
    g = s.get("guard") or {}
    of = g.get("on_fixed")
    if of is None:
        return ""
    passed, opened, broken = of.get("passed") or [], of.get("failed") or [], of.get("broken") or []
    total = len(passed) + len(opened) + len(broken)
    def rows(names, dot, word):
        return "".join(f'<div class="gh-check"><span class="gh-dot {dot}"></span><span class="gh-cname"><b>{e(str(n)[:200])}</b></span>'
                       f'<span class="gh-st {dot}">{word}</span></div>' for n in names)
    sib = g.get("siblings") or []
    body = (f'<div class="gh-annot muted"><b>What it is</b><p>One extra test with {total} cases. Each case is a different way '
            f'this kind of bug can happen. Every case failed on the old code, so the test would have caught this bug. On the '
            f'fixed code, a passing case is covered by the fix; a failing one is a gap the fix leaves open.</p>'
            f'<p><span class="gh-muted">What it checks:</span> {e(g.get("covers") or g.get("text") or "")}</p></div>'
            f'<div class="gh-box"><div class="gh-box-head"><span>{total} cases on the fixed code</span>'
            f'<span class="gh-muted">{len(passed)} covered · {len(opened)} still open · {len(broken)} broken</span></div>'
            f'{rows(opened, "bad", "Still open")}{rows(broken, "warn", "Broken")}{rows(passed, "ok", "Covered")}</div>'
            + (f'<div class="gh-box"><div class="gh-box-head"><span>The same code in {len(sib)} other file{"s" * (len(sib) != 1)}, not changed by this fix</span></div>'
               + "".join(f'<div class="gh-check"><span class="gh-cname"><code>{e(x)}</code></span></div>' for x in sib) + '</div>' if sib else "")
            + (f'<p class="gh-muted gh-fp">File <code>{e(g.get("repo_path", ""))}</code> · kept with the run, not added to the pull request.</p>'))
    meta = (f'<span class="gh-state ai">Written by AI</span>{len(passed)} of {total} cases covered by the fix'
            + (f' · {len(opened)} still open' if opened else ""))
    return _sheet("guardinfo", "Guard similar bugs", "Guard for similar bugs", meta, body)


def costs_sheet(d: dict, rows: list) -> str:
    """Cost details (PM review: the cost and token panel as a side panel): per step, then call by call."""
    use = usage_by_step(d)
    label = {k: lab for k, lab, _ in plain.STEPS}
    steps = "".join(f'<tr><td>{e(label.get(k, k))}</td><td>{u["calls"]}</td><td>{u["in"]:,}</td><td>{u["out"]:,}</td><td>${u["usd"]:.4f}</td></tr>'
                    for k, u in sorted(use.items(), key=lambda kv: [r["key"] for r in rows].index(kv[0]) if kv[0] in [r["key"] for r in rows] else 99))
    tot = {"calls": sum(u["calls"] for u in use.values()), "in": sum(u["in"] for u in use.values()),
           "out": sum(u["out"] for u in use.values()), "usd": sum(u["usd"] for u in use.values())}
    calls = "".join(f'<tr><td>{e(str(c.get("at", ""))[11:19])}</td><td>{e(label.get(c.get("step"), c.get("step") or ""))}</td>'
                    f'<td>{e(str(c.get("model", "")).split("/")[-1])}</td><td>{int(c.get("input_tokens") or 0):,}</td>'
                    f'<td>{int(c.get("output_tokens") or 0):,}</td><td>${(c.get("actual_micro") or 0) / 1e6:.4f}</td></tr>'
                    for c in d.get("calls") or [])
    body = (f'<div class="gh-box"><div class="gh-box-head"><span>By step</span></div><div class="dscroll"><table class="gh-table">'
            f'<tr><th>Step</th><th>AI calls</th><th>Tokens in</th><th>Tokens out</th><th>Cost</th></tr>{steps}'
            f'<tr class="tot"><td>Total</td><td>{tot["calls"]}</td><td>{tot["in"]:,}</td><td>{tot["out"]:,}</td><td>${tot["usd"]:.4f}</td></tr>'
            f'</table></div></div>'
            f'<details class="gh-fold"><summary>Call by call</summary><div class="dscroll"><table class="gh-table">'
            f'<tr><th>At</th><th>Step</th><th>Model</th><th>In</th><th>Out</th><th>Cost</th></tr>{calls}</table></div></details>'
            if tot["calls"] else '<p class="gh-muted">No AI calls yet.</p>')
    m = d.get("meter") or {}
    meta = (f'${tot["usd"]:.2f} of the ${m.get("cap_usd", 0) or 0:.2f} budget · {_k_fmt(tot["in"])} tokens in, '
            f'{_k_fmt(tot["out"])} out · compute {m.get("sandbox_used_s", 0) or 0} s')
    return (_sheet("costs", "Cost details", "Cost details", meta, body).replace('class="pop sheet gh"', 'class="pop sheet gh side"'))


def full_answer(raw: dict) -> str:
    """The advisors' server's whole answer, in plain sections, each folded; the JSON itself last."""
    mr = raw.get("machine_result") or {}
    tick = lambda ok: f'<span class="ok">{icons.check(14)}</span>' if ok else f'<span class="no">{icons.cross(14)}</span>'  # noqa: E731
    parts = [("Verdict", f'<p><b>{e(raw.get("verdict") or mr.get("state") or "none")}</b>'
                         + (f' · {e(mr.get("reason"))}' if mr.get("reason") else "")
                         + (f'<br>Needs: {e(", ".join(mr.get("needs") or []))}' if mr.get("needs") else "") + "</p>")]
    checks = mr.get("checks") or []
    if checks:
        parts.append(("Checks it ran", '<ul class="fa-list">' + "".join(
            f'<li>{tick(c.get("passed"))}<span><b>{e(str(c.get("id", "")).replace("_", " "))}</b> {e(c.get("detail", ""))}</span></li>'
            for c in checks) + "</ul>"))
    found = []
    if mr.get("blame_sentences") is not None:
        found.append(("Sentences read as blame", ", ".join(f'"{x}"' for x in mr["blame_sentences"]) or "none"))
    if mr.get("guard_kind"):
        m = mr.get("guard_patterns_matched") or {}
        found.append(("Guard kind", f'{mr["guard_kind"]} (condition phrases: {", ".join(m.get("condition") or []) or "none"}; '
                                    f'instruction phrases: {", ".join(m.get("instruction") or []) or "none"})'))
    for c in mr.get("candidates") or []:
        found.append(("Suspect kept", f'{c.get("path")}' + (f' lines {c["lines"]}' if c.get("lines") else "")
                                      + f' · confidence {c.get("confidence")} · {c.get("reason", "")}'))
    if mr.get("question"):
        found.append(("Missing fact to ask", f'{mr["question"]}' + (f' ({mr["answerer"]})' if mr.get("answerer") else "")))
    for k in ("is_defect", "confidence"):
        if k in mr:
            found.append(({"is_defect": "Likelihood of a defect", "confidence": "How sure"}[k], str(mr[k])))
    if found:
        parts.append(("What it found", "<dl class=\"fa-dl\">" + "".join(f"<dt>{e(a)}</dt><dd>{e(b)}</dd>" for a, b in found) + "</dl>"))
    if mr.get("rules"):
        parts.append(("Rules it applies", '<ul class="fa-plain">' + "".join(f"<li>{e(x)}</li>" for x in mr["rules"]) + "</ul>"))
    ft = raw.get("falsifiable_test") or {}
    if ft:
        parts.append(("How this answer will be tested", f'<p>{e(ft.get("statement", ""))}</p><p class="foot">{e(ft.get("resolve_rule", ""))}'
                                                        f' · now: {e(ft.get("state", ""))} · test {e(ft.get("test_id", ""))}</p>'))
    cal = raw.get("calibration") or {}
    if cal:
        parts.append(("Its record so far", f'<p>{e(cal.get("judgments", 0))} answers, {e(cal.get("resolved", 0))} checked against what '
                                           f'happened ({e(cal.get("passed", 0))} right, {e(cal.get("failed", 0))} wrong). '
                                           f'{e(cal.get("pass_rate_note") or "")}</p>'))
    parts.append(("Everything it sent (JSON)", f'<pre class="out">{e(json.dumps(raw, indent=1, default=str)[:16000])}</pre>'))
    ref = raw.get("judgment_id")
    return ('<div class="fa">' + (f'<p class="foot">Reference {e(ref)} · seat {e(raw.get("seat", ""))}</p>' if ref else "")
            + "".join(f'<details{" open" if i < 2 else ""}><summary>{e(t)}</summary>{body}</details>' for i, (t, body) in enumerate(parts))
            + "</div>")


def _advisors(s: dict) -> str:
    """Where an advisor seat reviews a step's output, and what happened: plain words, advice only."""
    from .advisors import REVIEWS, status
    now, done, rows, sheets = status()[0], s.get("advisors") or {}, [], []
    said = {"OFF": "Not asked: advisors are off (not connected yet)",
            "BLOCKED": "Not asked: the advisors' server has not been reviewed yet",
            "ON": "Will be asked when this step finishes", "FAILED": "Could not be reached; the run went on without it"}
    for step, r in REVIEWS.items():
        rec = done.get(step) or {}
        st = rec.get("status", now)
        sub = (f"Said: {rec.get('answer', '')[:240]}" if st == "ANSWERED" else said.get(st, "Not asked"))
        ic = icons.check() if st == "ANSWERED" else icons.pause() if st in ("OFF", "BLOCKED") else icons.cross() if st == "FAILED" else icons.list_(18)
        raw, pid = rec.get("raw"), f"advfull-{step}"
        btn = (f'<button type="button" class="tbtn proof-btn" popovertarget="{pid}">{icons.list_(14)}<span>Full answer</span></button>'
               if raw else "")
        rows.append(f'<li class="row {"done" if st == "ANSWERED" else "pending"}"><span class="ic">{ic}</span><span class="t">'
                    f'<b>{e(r["seat"])} reviews {e(r["what"])}</b><span>{e(sub)}</span></span><span class="tr">{btn}</span></li>')
        if raw:
            sheets.append(f'<div id="{pid}" popover class="pop sheet glass" aria-label="{e(r["seat"])}: full answer">'
                          f'<div class="pop-head"><b>{e(r["seat"])}: full answer</b><button type="button" class="tbtn" '
                          f'popovertarget="{pid}" popovertargetaction="hide" aria-label="Close">{icons.cross(16)}</button></div>'
                          f'{full_answer(raw)}</div>')
    return (f'<section class="group"><h2>Advisors</h2><div class="sect"><ul class="rows">{"".join(rows)}</ul></div>{"".join(sheets)}'
            '<p class="foot">Advice only. An advisor never changes the fix, the pull request text or your OK. They are '
            'switched on after the advisors\' server has been reviewed: <a href="/connect#advisors">how to connect them</a>.</p></section>')


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
    {f'<details><summary>Patch</summary>{diffview.html(d["patch"])}</details>' if d['patch'] else ''}</div>
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
# your OK from the page: only on served pages (a saved file keeps the terminal commands instead)
_DECIDE_JS = """
const commitMessage = () => {
  const t = document.getElementById("commit-title"), b = document.getElementById("commit-body");
  if (!t) return "";
  return (t.value.trim() + (b && b.value.trim() ? "\\n\\n" + b.value.trim() : "") + "\\n");
};
document.addEventListener("input", ev => {
  if (!["commit-title", "commit-body"].includes(ev.target.id)) return;
  const t = document.getElementById("commit-title"), b = document.getElementById("commit-body"), title = document.getElementById("pr-title");
  if (title) title.textContent = t.value.trim() || "(no title)";
  const same = t.value === t.dataset.recommended && b.value === b.dataset.recommended;
  const note = document.getElementById("commit-note");
  if (note) note.textContent = same ? "Recommended by DebugAssistAgent. Edit it: your message is what the commit carries, saved with your approval." : "Edited by you. This is the message the commit carries.";
});
document.addEventListener("click", ev => {
  if (ev.target.id !== "commit-reset") return;
  for (const id of ["commit-title", "commit-body"]) { const x = document.getElementById(id); x.value = x.dataset.recommended; }
  document.getElementById("commit-title").dispatchEvent(new Event("input", { bubbles: true }));
});
const TOKEN = __TOKEN__;
document.addEventListener("click", async ev => {   // Run again: the same issue, the same choices
  const b = ev.target.closest("[data-again]"); if (!b) return;
  b.disabled = true; const l = b.querySelector("span"); l.textContent = "Starting…";
  try {
    const r = await fetch("/api/start", { method: "POST", headers: { "X-DebugAssistAgent-Token": TOKEN, "Content-Type": "application/json" },
      body: b.dataset.again });
    const j = await r.json();
    if (!r.ok) { l.textContent = j.error || "Could not start."; b.disabled = false; return; }
    location.href = j.page;
  } catch (e) { l.textContent = "Could not reach DebugAssistAgent."; b.disabled = false; }
});
document.addEventListener("click", async ev => {
  const b = ev.target.closest("[data-decide]"); if (!b) return;
  const box = b.closest(".decide"), msg = box.querySelector(".decide-msg");
  if (b.dataset.armed !== "1") {                       // a second tap confirms: no decision on one stray click
    box.querySelectorAll("[data-decide]").forEach(x => { x.dataset.armed = ""; x.querySelector("span").textContent = x.dataset.label; });
    b.dataset.armed = "1"; b.querySelector("span").textContent = b.dataset.confirm;
    setTimeout(() => { if (b.dataset.armed === "1") { b.dataset.armed = ""; b.querySelector("span").textContent = b.dataset.label; } }, 5000);
    return;
  }
  box.querySelectorAll("[data-decide]").forEach(x => { x.disabled = true; });
  msg.textContent = b.dataset.decide === "approve" ? "Approving…" : "Closing…";
  try {
    const r = await fetch("/api/decide", { method: "POST", headers: { "X-DebugAssistAgent-Token": TOKEN, "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: box.dataset.run, decision: b.dataset.decide, sha256: box.dataset.sha,
                             commit_message: commitMessage() }) });
    const j = await r.json();
    if (!r.ok) { msg.textContent = j.error || "Something went wrong."; box.querySelectorAll("[data-decide]").forEach(x => { x.disabled = false; }); return; }
    msg.textContent = j.said;
  } catch (e) { msg.textContent = "Could not reach DebugAssistAgent."; box.querySelectorAll("[data-decide]").forEach(x => { x.disabled = false; }); }
});
"""

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
        const open = o.matches && o.matches(":popover-open"), fresh = document.importNode(n, true);
        o.replaceWith(fresh);
        if (open && fresh.showPopover) { try { fresh.showPopover(); } catch (e) {} }
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
