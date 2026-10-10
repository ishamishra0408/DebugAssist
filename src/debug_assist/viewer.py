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


def answered(ask: dict, evs: list[dict], since: str | None) -> dict | None:
    """The answer to "install this package?", once given (Isha 2026-10-10: "when I click install, it should show that
    it is now installing"). The run's checkpoint keeps the question until Show the bug ends, minutes later, so the
    answer is read from the events: the page's own record, written as you tap, or the run's. installing: yes, and the
    test machine is not rebuilt yet."""
    if ask.get("kind") == "advisor":   # whose plan to follow: the page's record as you tap, or the run's
        for x in reversed(evs):
            mine = x.get("reviewed") == ask.get("step") and (
                (x.get("kind") == "decision" and str(x.get("decision", "")).startswith("advisor ")) or x.get("kind") == "advisor_choice")
            if mine and (since is None or _t(x["at"]) >= _t(since)):
                said = x.get("choice") or str(x.get("decision", "")).split(" ", 1)[-1]
                return {"kind": "advisor", "answer": said, "at": x["at"], "installing": False}
        return None
    pkg = ask.get("package")
    if ask.get("kind") != "install" or not pkg:
        return None
    for x in reversed(evs):   # events come without their key; the fields tell them apart
        mine = x.get("package") == pkg and ((x.get("kind") == "decision" and str(x.get("decision", "")).startswith("install"))
                                            or (x.get("kind") == "install" and x.get("answer")))
        if mine and (since is None or _t(x["at"]) >= _t(since)):
            yes = "yes" in str(x.get("decision") or x.get("answer") or "")
            built = any(y.get("kind") == "install" and y.get("package") == pkg and "seconds" in y and _t(y["at"]) >= _t(x["at"])
                        for y in evs)
            return {"package": pkg, "answer": "yes" if yes else "no", "at": x["at"], "minutes": ask.get("minutes", 3),
                    "installing": yes and not built}
    return None


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
    told = answered(intr, evs, snap.created_at)
    if told:   # answered: the run is going again, though its checkpoint still holds the question
        intr = {}
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
            "ask": intr if intr.get("kind") == "install" else None,   # the run asks whether to install a package
            "advisor_ask": intr if intr.get("kind") == "advisor" else None,   # an advisor disagreed: whose plan?
            "installing": told if told and told["installing"] else None,
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


def fresh_line(s: dict) -> str:
    """Which code the run is on (Isha 2026-10-10): branched from main, its commit and when it was fetched, so no one
    wonders whether it is behind. Runs before then used the saved copy, and say so."""
    code = s.get("code") or {}
    repo = (s.get("profile") or {}).get("repo", "")
    if code.get("commit"):
        when = code.get("fetched_at", "")
        try:
            when = datetime.fromisoformat(when).strftime("%-d %b, %H:%M UTC")
        except ValueError:
            pass
        link = (f'<a href="https://github.com/{e(repo)}/commit/{e(code["commit"])}" target="_blank" rel="noopener noreferrer">'
                f'<code>{e(code["commit"][:7])}</code></a>' if re.fullmatch(r"[\w.-]+/[\w.-]+", repo) else f'<code>{e(code["commit"][:7])}</code>')
        extra = (" · the test machine was rebuilt (the package list changed)" if code.get("rebuilt") else
                 f" · {len(code['build'])} changed package{'s' * (len(code['build']) != 1)} rebuilt" if code.get("build") else "")
        return (f'<p class="fresh"><span class="fresh-dot"></span>Branched from <b>{e(code.get("branch", "main"))}</b> at {link} · '
                f'fetched {e(when)}, the latest when this run started{e(extra)}</p>')
    if code.get("error"):
        return f'<p class="fresh warn"><span class="fresh-dot"></span>{e(code["error"])}</p>'
    if s.get("issue"):
        return '<p class="fresh muted"><span class="fresh-dot"></span>On the saved copy of the code (runs before 10 Oct 2026)</p>'
    return ""


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
    inst = d.get("installing") if phase == "working" else None   # you said yes; the test machine is being rebuilt
    status_line = ("Waiting for your answer: install a package?" if d.get("ask") and phase == "waiting" else
                   "Waiting for you: an advisor disagrees" if d.get("advisor_ask") and phase == "waiting" else
                   f"Installing {inst['package']} on the test machine" if inst else headline(phase, cur, rows))
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
    last_ev = d["events"][-1]["at"] if d["events"] else None   # Isha 2026-10-09: beside the issue, not a tile
    last_seen = f' · Last event {_since(d, last_ev, is_replay, " ago")}' if last_ev and issue else ""
    nodes = "".join(
        f'<li class="node {st}"><span class="bead">{icons.STATE[st](16) if st in icons.STATE else i + 1}</span>'
        f'<span class="nl">{e(r["label"])}</span></li>' for i, (r, st) in enumerate(zip(rows, states)))
    replay_bar = (f'<div class="replay-bar">{icons.play(16)}<span>Replay of a past run, sped up {rp.get("speed", 8):g}×. '
                  f'Nothing is running. {rp.get("elapsed", 0):.0f} of {rp.get("length", 0):.0f} s.</span></div>' if is_replay else "")
    hero = f"""<header class="hero">
  {replay_bar}
  <p class="eyebrow">{f'{e(who)} · Issue #{e(issue.get("number"))} · {e(model)}' if issue else 'New run'}{last_seen}</p>
  <h1>{e(issue.get('title') or ('Getting ready' if not s else 'Run ' + rid))}</h1>
  <p class="status {cls}" role="status"><span class="dot"></span><span>{e(status_line)}</span>{f'<span class="sub">· {_since(d, running_since, is_replay)}</span>' if running_since else ''}</p>
  {fresh_line(s)}
  <ol class="track" style="--n:{len(rows)}" aria-label="The {len(rows)} steps">{nodes}</ol>
</header>"""

    # ── the steps, one row each: what it gave, or what is happening in it ──
    use = usage_by_step(d)
    proof_html = _proof(s, rid, ai_use(use, "reproduce"))
    pr_btn = '<button type="button" class="tbtn proof-btn" popovertarget="check-pr"><span>Check PR</span></button>'
    prof = s.get("profile") or {}
    ctx_html = context_sheet(d.get("pack") or {}, s.get("context") or {}, prof.get("repo") or "",
                             (s.get("code") or {}).get("commit") or prof.get("base_commit") or "")
    change_html = code_change_sheet(s, rid, d.get("patch") or "", ai_use(use, "find_cause", "write_fix"))
    report_html, guard_html = report_sheet(s, ai_use(use, "why_it_shipped")), guard_sheet(s, ai_use(use, "lasting_guard"))
    def btn(target: str, label: str) -> str:
        return f'<button type="button" class="tbtn proof-btn" popovertarget="{target}"><span>{e(label)}</span></button>'
    issue_link = (f'<a class="tbtn proof-btn" href="{e(s["issue_url"])}" target="_blank" rel="noopener noreferrer"><span>Open issue</span></a>'
                  if str(s.get("issue_url", "")).startswith("https://github.com/") else "")
    step_btns = {"read_issue": issue_link, "gather_context": btn("ctxinfo", "Context info") if ctx_html else "",
                 "reproduce": btn("proof", "Check proof") if proof_html else "",
                 "find_cause": btn("codechange", "Code change") if change_html else "",
                 "why_it_shipped": btn("report", "Read report") if report_html else "",
                 "lasting_guard": btn("guardinfo", "Guard") if guard_html else ""}
    step_rows_html = []
    for i, (r, st) in enumerate(zip(rows, states)):
        chips, cnt = inside(r["key"], d["events"])
        if st == "done":
            sub = plain.result(r["key"], s) or (chips[-1] if chips else "Done")
            trail = e(plain.duration(secs[r["key"]])) if secs.get(r["key"]) is not None else ""
            trail = step_btns.get(r["key"], "") + trail
            if r["key"] == "approval" and d["pr_text"]:
                trail = pr_btn + trail
        elif st == "running" and inst and r["key"] == "reproduce":
            sub = f"Installing {inst['package']} on the test machine · about {inst['minutes']} minutes"
            trail = _since(d, inst["at"], is_replay)
        elif st == "running":
            sub = " · ".join(chips[-2:] + ([cnt] if cnt else [])) or "Starting this step"
            trail = _since(d, d.get("since"), is_replay)
        elif st == "waiting" and d.get("advisor_ask"):   # Isha 2026-10-10: an advisor disagrees; whose plan?
            aa = d["advisor_ask"]
            sub = f"Waiting for you: the {aa.get('role')} advisor disagrees: {aa.get('why')}. Choose whose plan to follow."
            trail = '<button type="button" class="tbtn proof-btn" popovertarget="advisorask"><span>Choose</span></button>'
        elif st == "waiting" and d.get("ask"):   # Isha 2026-10-10: the package isn't installed; may it be?
            sub = f"Waiting for you: {d['ask'].get('package')} isn't installed on the test machine. Install it?"
            trail = '<button type="button" class="tbtn proof-btn" popovertarget="installask"><span>Answer</span></button>'
        elif st == "waiting":
            sub = ("Check the pull request, then approve it or close it" if served and not is_replay else
                   "Check the pull request, then approve or say no in your terminal")
            trail = pr_btn if d["pr_text"] else ""
        elif st == "stopped":
            sub = plain.exit_text(outcome.get("exit")).removeprefix("Stopped. ") if outcome else "Stopped here"
            trail = step_btns.get(r["key"], "") + "Stopped"
        elif st == "skipped":
            sub, trail = "Not in this run. This step was added after it ran.", ""
        else:
            sub, trail = gives[r["key"]], ""
        ic = icons.STATE[st]() if st in icons.STATE else f'<span class="num">{i + 1}</span>'
        step_rows_html.append(_k(f"step-{i}", f'<li class="row {st}"><span class="ic">{ic}</span><span class="t"><b>{e(r["label"])}</b>'
                                 f'<span>{e(sub)}</span></span><span class="tr">{trail}</span></li>'))

    # ── latest activity ──
    label = {k: lab for k, lab, _ in plain.STEPS}
    acts = "".join(f'<li><span class="gh-dot muted"></span><span class="gh-cname"><b>{e(plain.activity(x))}</b>'
                   f'<span class="gh-muted">{e(label.get(x.get("step"), x.get("step") or ""))}</span></span>'
                   f'<time class="gh-muted">{e(x["at"][11:19])}</time></li>' for x in reversed(d["events"][-12:]))

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
    calls = d.get("calls") or [x for x in d["events"] if x.get("kind") == "model_call"]   # the metered calls
    tok_in, tok_out = (sum(int(x.get(k) or 0) for x in calls) for k in ("input_tokens", "output_tokens"))
    tiles = [("Time to fix", proven, note),
             ("LLM cost", f"${spent:.4f}" if 0 < spent < 0.1 else f"${spent:.2f}", f"Budget ${cap:.2f}" if cap else ""),
             ("Tokens", f"{_k_fmt(tok_in)} in · {_k_fmt(tok_out)} out", f"{len(calls)} model call{'s' * (len(calls) != 1)}"),
             ("Compute time", f"{m.get('sandbox_used_s', 0) or 0} s", f"Quota {int(m.get('sandbox_cap_s') or 1800) // 60} min")]
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
            "working": ((f"Installing {inst['package']}", f"The test machine is being rebuilt with it, about "
                         f"{inst['minutes']} minutes. Then the run goes on. This page updates by itself.", "")
                        if inst else ("Nothing to do right now", "This page updates by itself.", "")),
            "interrupted": ("Interrupted", f"Nothing has happened for {quiet}. Continue it from your terminal.", _copy(resume, "Copy resume command", True)),
            "crashed": ("Crashed", "Continue it from your terminal.", _copy(resume, "Copy resume command", True)),
            "waiting": (("Install a package?", f"{(d.get('ask') or {}).get('package')} isn't installed on the test machine, so "
                         "its tests can't run.", '<button type="button" class="btn glass prominent" popovertarget="installask">'
                         '<span>Answer</span></button>' if served else
                         _copy(f"cd ~/Projects/DebugAssist && uv run debug-assist answer {rid} yes", "Copy install command", True))
                        if d.get("ask") else
                        ("An advisor disagrees", f"The {(d.get('advisor_ask') or {}).get('role')} advisor disagrees: "
                         f"{(d.get('advisor_ask') or {}).get('why')}. Choose whose plan to follow.",
                         '<button type="button" class="btn glass prominent" popovertarget="advisorask"><span>Choose</span></button>'
                         if served else _copy(f"cd ~/Projects/DebugAssist && uv run debug-assist answer {rid} run",
                                              "Copy keep-the-run's-plan command", True))
                        if d.get("advisor_ask") else
                        ("Your OK is needed", "Check the pull request, then approve it or close it. Nothing is posted to GitHub.",
                         '<button type="button" class="btn glass prominent" popovertarget="check-pr"><span>Check PR</span></button>')),
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
    topo = ('<script src="/static/topo.js" defer></script><script src="/static/glass.js" defer></script>'
            '<script src="/static/advisors.js" defer></script>' if served else
            "".join(f"<script>{(STATIC / f).read_text()}</script>" for f in ("topo.js", "glass.js", "advisors.js")))
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
<title>{e("Waiting for your answer" if (d.get("ask") or d.get("advisor_ask")) and phase == "waiting" else status_line)} · {plain.NAME}</title>
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
<section class="metrics" aria-label="Usage and cost"><div class="tiles">{tiles_html}</div></section>
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
{_k("sheet-ask", install_sheet(d, rid, served and not is_replay) if d.get("ask") and phase == "waiting" else '<i id="installask" hidden></i>')}
{_k("sheet-adv", advisor_sheet(d, rid, served and not is_replay) if d.get("advisor_ask") and phase == "waiting" else '<i id="advisorask" hidden></i>')}
{_k("sheet-ctx", ctx_html or '<i id="ctxinfo" hidden></i>')}
{_k("sheet-change", change_html or '<i id="codechange" hidden></i>')}
{_k("sheet-report", report_html or '<i id="report" hidden></i>')}
{_k("sheet-guard", guard_html or '<i id="guardinfo" hidden></i>')}
<div id="acts" popover class="pop gh gh-menu" aria-label="Activity">
  <div class="gh-menu-head"><b>Activity</b><button type="button" class="gh-close" popovertarget="acts" popovertargetaction="hide" aria-label="Close">{icons.cross(16)}</button></div>
  {_k("log", f'<ul class="gh-activity">{acts or "<li><span class=gh-muted>Nothing yet</span></li>"}</ul>')}
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
                    "fail" if ex.get("status") in ("FOUND", "OTHER FAILURES", "CAN'T TELL") else "none",
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
    based = (f'<span class="gh-muted">· branched from main at {e(str(st["code"]["commit"])[:7])}</span>'
             if (st.get("code") or {}).get("commit") else "")
    return (f'<div id="check-pr" popover class="pop sheet gh" aria-label="Pull request">'
            f'<div class="gh-top"><button type="button" class="gh-close" popovertarget="check-pr" popovertargetaction="hide" '
            f'aria-label="Close">{icons.cross(16)}</button>'
            f'<h3 class="gh-title"><span id="pr-title">{e(title)}</span> <span class="gh-muted">#{e(issue.get("number", ""))}</span></h3>'
            f'<p class="gh-meta">{badge}<b>{who}</b> wants to merge 1 commit into '
            f'<code class="gh-ref">main</code> from <code class="gh-ref">{e(head)}</code>{based}'
            f'<span class="gh-diffstat"><span class="plus">+{add}</span> <span class="minus">−{rem}</span></span></p>'
            f'{ai_use(usage_by_step(d), "find_cause", "write_fix", what=": cause and fix")}'
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


def _proof(s: dict, rid: str, ai: str = "") -> str:
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
    elif ex.get("status") == "CAN'T TELL":   # review of run #22085: said as such, never as "not for this issue"
        ans, dot, why = "Can't tell", "muted", (f"{ex.get('failed', 'Some')} of the repo's own tests in {pkg} fail on main. The "
                                                "issue quotes no code, so whether any of them fails for this issue can't be "
                                                "told. A test was written for it.")
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
        from .testwriter import NOT_CHECKED
        unchecked = NOT_CHECKED in (first.get("evidence") or "")
        gist = (f'<div class="gh-annot muted"><b>In short</b><p>{e(before)}A new {e(kind)} written for this issue (try {first["n"]}) '
                + ("fails on main on an assertion. The issue quotes no code, so whether that failure is the issue's own "
                   "symptom was judged by the failing assertion only." if unchecked else
                   "fails on main for the reason the issue describes.")
                + f' With the fix: {e(aw[0].lower() + aw[1:])}.</p></div>')
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
    base = str((s.get("code") or {}).get("commit") or prof.get("base_commit") or "")[:7]
    title = (f'<h3 class="gh-title">Reproduce issue <span class="gh-muted">#{e(issue.get("number", ""))}</span></h3>'
             f'<p class="gh-meta"><span class="gh-state failure">Bug shown</span> on <code class="gh-ref">main</code>'
             + (f' at <code class="gh-ref">{e(base)}</code>' if base else "")
             + (f' · try {shown} of {len(tries)}' if shown else "") + ' · run by DebugAssistAgent, internet off</p>' + ai)
    return (f'<div class="proof gh-proof">{title}<h4 class="gh-h">Summary</h4>{summary}{pager}<p class="gh-muted gh-fp">The code '
            'was the repository as it is, with only the test added. A test that fails for a different reason does not count '
            'as showing the bug.</p></div>')


# ── sheets that open from the steps (Isha 2026-10-09): Context info, Read report, Guard ─────────────────────────
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


def ai_use(use: dict, *steps: str, what: str = "") -> str:
    """The AI this step used, under its sheet's title (Isha 2026-10-09: inside the dialog, not on the step row)."""
    us = [use[k] for k in steps if k in use]
    calls, tin, tout, usd = (sum(u[k] for u in us) for k in ("calls", "in", "out", "usd"))
    if not calls:
        return ""
    return (f'<p class="gh-ai"><span class="Label">AI usage{e(what)}</span>{_k_fmt(tin + tout)} tokens · ${usd:.2f} · '
            f'{calls} request{"s" * (calls != 1)} <span class="gh-muted">· {tin:,} input, {tout:,} output</span></p>')


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


ADVISOR_COST = {"read_issue": "Stops the run", "find_cause": "+1 AI call", "why_it_shipped": "+1 AI call",
                "lasting_guard": "+1 AI call, tests rerun"}   # what following the advisor costs


def _who(seat: str) -> dict:
    """An advisor's identity, as the Connect page draws it: role, motto, palette."""
    from .advisors import ASK
    w = ASK.get(seat) or {}
    return {"role": w.get("role", "Advisor"), "motto": w.get("motto", ""), "palette": w.get("palette", "tide")}


# An advisor's seal: engraved line-work in its colour, one pattern per advisor (Isha 2026-10-10: "the balls are not
# pretty; take inspiration from ThreeUI's components": its Engraved Certificate's guilloche rings). Each is drawn from a
# few SVG shapes, so it stays small; the outer ring turns slowly (still with Reduce Motion).
def _hexagon(r: float, turn: float = 0) -> str:
    import math
    pts = " ".join(f"{16 + r * math.cos(math.radians(60 * i + turn)):.2f},{16 + r * math.sin(math.radians(60 * i + turn)):.2f}"
                   for i in range(6))
    return f'<polygon points="{pts}"/>'


SEAL_MOTIF = {
    # Triage: a rosette of currents, sorted around one centre
    "tide": "".join(f'<ellipse cx="16" cy="16" rx="10.4" ry="3.9" transform="rotate({i * 30} 16 16)"/>' for i in range(6))
            + '<circle cx="16" cy="16" r="2.2" class="fill"/>',
    # Localization: rings closing on a point, a crosshair through it
    "violet": "".join(f'<circle cx="16" cy="16" r="{r}"/>' for r in (3.2, 6.4, 9.6))
              + '<path d="M16 3.5v6.2M16 22.3v6.2M3.5 16h6.2M22.3 16h6.2"/><circle cx="16" cy="16" r="1" class="fill"/>',
    # Incident review: three conditions overlapping, none of them a person
    "ember": "".join(f'<circle cx="{16 + 4.1 * c:.2f}" cy="{16 + 4.1 * s_:.2f}" r="6.1"/>'
                     for c, s_ in ((0, -1), (0.866, 0.5), (-0.866, 0.5)))
             + '<circle cx="16" cy="16" r="1.3" class="fill"/>',
    # Quality gate: nested hexagons, a gate through the middle
    "moss": _hexagon(10.8, 30) + _hexagon(7.6, 30) + _hexagon(4.4, 30)
            + '<path d="M13.6 11.4v9.2M18.4 11.4v9.2"/>',
}


def _seal(seat: str) -> str:
    pal = _who(seat)["palette"]
    return (f'<span class="adv-seal pal-{e(pal)}" aria-hidden="true"><svg viewBox="0 0 32 32" fill="none" stroke="currentColor">'
            f'<g class="ring"><circle cx="16" cy="16" r="14.6"/><circle cx="16" cy="16" r="13.2" class="ticks"/></g>'
            f'<g class="motif">{SEAL_MOTIF.get(pal, SEAL_MOTIF["tide"])}</g></svg></span>')


def advisor_sheet(d: dict, rid: str, can_answer: bool) -> str:
    """An advisor disagrees (Isha 2026-10-10): the run's plan and the advisor's, facing each other; you choose, then it
    builds. Isha 2026-10-10, second pass: "too bland; take inspiration from the UI references" (21st.dev, GSAP,
    tasteskill, ThreeUI Living Green): the advisor's living scene heads it, in its own colour, as on the Connect page."""
    a = d.get("advisor_ask") or {}
    steps = dict((k, v) for k, v, _ in plain.STEPS)
    step_label = steps.get(a.get("step"), a.get("step"))
    waits = steps.get((d.get("next") or [""])[0], "the next step")
    w = _who(a.get("seat", ""))
    why = str(a.get("why", ""))
    hero = (f'<div class="adv-hero pal-{e(w["palette"])}"><canvas class="sigil" data-seat="{e(a.get("seat"))}" '
            f'data-palette="{e(w["palette"])}" aria-hidden="true"></canvas>'
            f'<div class="adv-hero-id"><span class="adv-role">{e(w["role"])} · after {e(step_label)}</span>'
            f'<b>{e(a.get("seat"))}</b><span class="adv-hero-motto">\u201c{e(w["motto"])}\u201d</span></div>'
            f'<span class="adv-verdict">Disagrees</span></div>')
    point = (f'<blockquote class="adv-point"><span class="k">Where it differs</span>{e(why[:1].upper() + why[1:])}.'
             f'</blockquote>')
    def card(side: str, who: str, cost: str, plan: str, then: str, btn: str) -> str:
        return (f'<article class="adv-choice {side}"><header><span class="adv-who"><i></i>{who}</span>'
                f'<span class="adv-cost">{e(cost)}</span></header><p class="adv-plan">{e(plan)}</p>'
                f'<p class="adv-then">{e(then)}</p>{btn}</article>')
    run_btn = ('<button type="button" class="gh-btn" data-install="run" data-label="Keep the run\'s plan" '
               'data-confirm="Tap again to keep it" data-busy="Going on…"><span>Keep the run\'s plan</span></button>'
               if can_answer else f'<div class="cmd"><code>uv run debug-assist answer {e(rid)} run</code></div>')
    adv_btn = ('<button type="button" class="gh-btn adv-go-btn" data-install="advisor" data-label="Go with the advisor" '
               'data-confirm="Tap again to follow it" data-busy="Following the advisor…"><span>Go with the advisor</span></button>'
               if can_answer else f'<div class="cmd"><code>uv run debug-assist answer {e(rid)} advisor</code></div>')
    vs = (card("ours", "The run", "No extra cost", a.get("run_plan", ""), "If you keep it, the run goes on as planned.", run_btn)
          + '<div class="adv-vs-mid" aria-hidden="true"><span>VS</span></div>'
          + card("theirs", e(w["role"]) + " advisor", ADVISOR_COST.get(a.get("step"), "+1 AI call"), a.get("said", ""),
                 f'If you go with it: {a.get("if_advisor", "")}', adv_btn))
    body = (f'<div class="adv-dlg pal-{e(w["palette"])}">{hero}{point}'
            + (f'<div class="decide adv-decide" data-run="{e(rid)}" data-api="/api/answer"><div class="adv-vs">{vs}</div>'
               f'<p class="decide-msg" role="status"></p></div>' if can_answer else f'<div class="adv-vs">{vs}</div>')
            + f'<p class="adv-dlg-foot">{e(waits)} waits for you. The advisor is a rule check on the Domain Expertise MCP '
              f'server, not an AI; only our own files and sentences can come back from it.</p></div>')
    return _sheet("advisorask", "An advisor disagrees", f"The {e(w['role'])} advisor disagrees",
                  f'<span class="Label Label--attention">Needs your choice</span>{e(step_label)} is reviewed; {e(waits)} waits', body)


def install_sheet(d: dict, rid: str, can_answer: bool) -> str:
    """The question (Isha 2026-10-10): the package the bug is in isn't installed on the test machine; install it?
    Yes rebuilds the test machine with it, for this and every later run of the repo; No stops, nothing spent."""
    a = d.get("ask") or {}
    pkg, folder, mins = a.get("package", ""), a.get("dir", ""), a.get("minutes", 3)
    body = (f'<div class="gh-annot warn"><b>{e(pkg)} isn\'t installed on the test machine</b><p>The bug is in '
            f'<code class="gh-ref">{e(folder)}</code>, but that package isn\'t on the test machine, so none of its tests '
            f'can run, and a test written for this bug couldn\'t either. Nothing has been spent on writing tests yet.</p></div>'
            f'<div class="gh-box"><div class="gh-box-head"><span>If you install it</span></div>'
            f'<div class="gh-check"><span class="gh-cname"><b>The test machine is rebuilt with {e(pkg)}</b><span class="gh-muted">about '
            f'{e(mins)} minutes and a few cents of E2B credit; runs have no internet, so this is how a package is added</span></span></div>'
            f'<div class="gh-check"><span class="gh-cname"><b>The run goes on to Show the bug</b><span class="gh-muted">with the '
            f'package\'s own tests first</span></span></div>'
            f'<div class="gh-check"><span class="gh-cname"><b>It stays installed</b><span class="gh-muted">every later run of '
            f'{e(a.get("repo", "this repo"))} has it; no one is asked again</span></span></div></div>')
    if can_answer:
        body += (f'<div class="decide gh-merge" data-run="{e(rid)}" data-ask="install">'
                 f'<div class="gh-merge-lines"><b>Install {e(pkg)}?</b><span class="gh-muted">If you say no, the run stops '
                 f'here with nothing spent.</span></div><div class="gh-merge-btns">'
                 f'<button type="button" class="gh-btn danger" data-install="no" data-label="No, stop the run" '
                 f'data-confirm="Confirm: stop the run"><span>No, stop the run</span></button>'
                 f'<button type="button" class="gh-btn primary" data-install="yes" data-label="Install and continue" '
                 f'data-confirm="Confirm: install"><span>Install and continue</span></button></div>'
                 f'<p class="decide-msg" role="status"></p></div>')
    else:
        body += (f'<div class="gh-box"><div class="cmd"><code>cd ~/Projects/DebugAssist && uv run debug-assist answer {e(rid)} yes</code></div>'
                 f'<div class="cmd"><code>cd ~/Projects/DebugAssist && uv run debug-assist answer {e(rid)} no</code></div></div>')
    return _sheet("installask", "Install a package?", f"Install {e(pkg)}?",
                  '<span class="Label Label--attention">Needs your answer</span>Show the bug is waiting for you', body)


def _gh_link(repo: str, path: str, text: str, cls: str = "") -> str:
    """A link to the repo on GitHub, in a new tab; plain text when the repo is unknown."""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo or "") or not re.fullmatch(r"[\w./@+-]+", path or ""):
        return f'<span class="{cls}">{e(text)}</span>' if cls else e(text)
    return (f'<a class="gh-a {cls}" href="https://github.com/{e(repo)}/{e(path)}" target="_blank" rel="noopener noreferrer" '
            f'onclick="event.stopPropagation()">{e(text)}</a>')


def context_sheet(pack: dict, c: dict, repo: str = "", base: str = "") -> str:
    """Everything Gather context read, GitHub's way (Isha 2026-10-09): the comments as comments, the history as a
    commit list, the code as files with the issue's strings marked, the shared code. Collected by code, not AI."""
    if not pack:
        return ""
    iss, code = pack.get("issue") or {}, pack.get("code") or {}
    ctx = code.get("ctx") or {}
    cs, links = iss.get("comments") or [], iss.get("linked") or []
    fixes = {p["number"]: p.get("files") or [] for p in iss.get("fix_prs") or []}   # PRs read in full (review of #22543)
    conv = "".join(f'<div class="gh-comment"><div class="gh-comment-head"><b>Comment {i}</b> · commented on {e(str(x.get("at") or "")[:10])}</div>'
                   f'{_md(x.get("text", ""))}</div>' for i, x in enumerate(cs, 1)) or '<p class="gh-muted">The issue has no comments.</p>'
    if links:
        conv += ('<div class="gh-box"><div class="gh-box-head"><span>Linked issues and pull requests</span></div>'
                 + "".join(f'<div class="gh-check"><span class="gh-dot {"ok" if x.get("merged") else "muted"}"></span>'
                           f'<span class="gh-cname"><b>{_gh_link(x.get("repo", ""), ("pull/" if x.get("pull_request") else "issues/") + str(x.get("number", "")), x.get("title", ""))}</b>'
                           f'<span class="gh-muted">{e(x.get("repo", ""))}#{e(x.get("number", ""))} · '
                           f'{"pull request" if x.get("pull_request") else "issue"} · {e(x.get("state", ""))}{" · merged" if x.get("merged") else ""}'
                           f'{" · " + e(x["found"]) if x.get("found") else ""}'
                           f'{" · changes " + e(", ".join(Path(f).name for f in fixes[x.get("number")][:4])) if fixes.get(x.get("number")) else ""}</span></span></div>'
                           for x in links) + '</div>')
    errs = (iss.get("errors") or {})
    if errs.get("errors") or errs.get("frames"):
        conv += f'<div class="gh-box"><div class="gh-box-head"><span>Errors quoted in the issue</span></div><pre class="gh-pre">{e(chr(10).join(errs.get("errors", []) + ["at " + f for f in errs.get("frames", [])]))}</pre></div>'
    hist = pack.get("history") or []
    commits = "".join(
        f'<div class="gh-box"><div class="gh-box-head"><span>Commits on <code>{e(h["path"])}</code></span></div>'
        + ("".join(f'<div class="gh-commit-row"><span class="gh-cname"><b>{_gh_link(repo, "commit/" + str(x.get("sha", "")), x.get("title", ""))}</b>'
                   f'<span class="gh-muted">committed on {e(x.get("date", ""))}</span></span>'
                   f'{_gh_link(repo, "commit/" + str(x.get("sha", "")), str(x.get("sha", ""))[:7], "gh-sha")}</div>'
                   for x in h.get("changes") or []) or '<p class="gh-muted gh-pad">No recent changes found.</p>') + '</div>'
        for h in hist) or '<p class="gh-muted">No history was read.</p>'
    ranking = code.get("ranking") or []
    files = ""
    for i, f in enumerate(ranking):
        reasons = [r[0] for r in (f.get("reasons") or [])[:3]]   # what really ranked it (F36), heaviest first
        why = (" · ".join(reasons) if reasons else
               "contains " + (", ".join(f"“{a}”" for a in (f.get("matched") or [])[:4]) or "the problem's words"))
        if f.get("tied_with"):
            why += f" · tied (score {f.get('score')}) with {', '.join(Path(x).name for x in f['tied_with'])}: ordered by file name"
        lines = ctx.get("snippets", "") if f["path"] == ctx.get("source") and ctx.get("snippets") else f.get("snippets", "")
        head = (f'<span class="dpath">{e(f["path"])}</span>'
                f'<span class="dtag">{"best match · " if i == 0 else ""}{e(why)}</span>'
                + (_gh_link(repo, f"blob/{base}/{f['path']}", "View on GitHub", "gh-ext") if base else ""))
        files += (f'<details class="dfile"{" open" if i == 0 else ""}><summary>{head}</summary>{_blob(lines, f.get("matched") or [])}</details>'
                  if lines else   # nothing kept to show: a plain row, not one that looks like it opens
                  f'<div class="dfile"><div class="dfile-head">{head}</div></div>')
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
    par, hp = code.get("parallels") or [], code.get("helpers") or []
    if par:   # how the repo already does the same thing (review of run #22543)
        shared += ('<p class="gh-muted gh-pad">The same file in other packages</p>' + "".join(
            f'<details class="dfile"><summary><span class="dpath">{e(x["path"])}</span><span class="dtag">how this repo '
            f'already does it</span></summary>{_blob(x.get("snippets", ""), [])}</details>' for x in par))
    if hp:
        shared += ('<p class="gh-muted gh-pad">Helpers the repo already exports for these names</p>' + "".join(
            f'<details class="dfile"><summary><span class="dpath">{e(h["name"])}</span><span class="dtag">{e(h["path"])}:{e(h["line"])}'
            f'</span></summary><pre class="gh-pre">{e(str(h.get("definition", ""))[:3000])}</pre></details>' for h in hp))
    n = pack.get("counts") or {}
    def many(k, one, more):
        return f"{k} {one if k == 1 else more}"
    meta = (f'<span class="Label">No AI</span><b>debugassist</b> read {many(n.get("comments", len(cs)), "comment", "comments")}, '
            f'{many(n.get("files", len(ranking)), "file", "files")}, {many(n.get("related", len(rel)), "piece", "pieces")} of shared code and '
            f'{many(n.get("changes", 0), "recent change", "recent changes")} · locked at <code class="gh-ref">{e(str(c.get("sha256", ""))[:12])}</code>')
    panes = (f'<div class="pr-pane" data-pane="conv">{conv}</div><div class="pr-pane" data-pane="commits" hidden>{commits}</div>'
             f'<div class="pr-pane" data-pane="code" hidden>{files}</div><div class="pr-pane" data-pane="shared" hidden>{shared}</div>'
             f'<p class="gh-muted gh-fp">Show the bug, Find the cause and Fix it read only this context.</p>')
    return _sheet("ctxinfo", "Context info", f'Context for issue <span class="gh-muted">#{e(iss.get("number", ""))}</span>', meta, panes,
                  [("conv", "Conversation", len(cs)), ("commits", "Commits", sum(len(h.get("changes") or []) for h in hist)),
                   ("code", "Code", len(ranking)), ("shared", "Shared code", len(rel) + len(par) + len(hp))])


def _file_at_fault(rid: str, path: str) -> str:
    """The file as the pinned commit has it (before any fix), from the run's own copy of the code."""
    import subprocess
    co = _runs_dir() / rid / "checkout"
    if not path or not (co / ".git").exists():
        return ""
    r = subprocess.run(["git", "-C", str(co), "show", f"HEAD:{path}"], capture_output=True, text=True, timeout=30)
    return r.stdout if r.returncode == 0 else ""


def code_change_sheet(s: dict, rid: str, fix_patch: str, ai: str = "") -> str:
    """Code change (Isha 2026-10-09, F32 part A, view only): the code at fault as GitHub shows a file, the lines named
    highlighted, why they are the cause and the plan; then the fix, git style, once Fix it has written it."""
    c = s.get("cause") or {}
    if c.get("status") != "FOUND" or not c.get("file"):
        return ""
    a, b = (c.get("lines") or [0, 0])[:2]
    text = _file_at_fault(rid, c["file"])
    rows = ""
    if text:
        src = text.splitlines()
        lo, hi = max(1, a - 12), min(len(src), b + 12)
        rows = "".join(f'<tr class="{"hit" if a <= n <= b else ""}"><td class="ln">{n}</td><td class="code">{e(src[n - 1])}</td></tr>'
                       for n in range(lo, hi + 1))
    code = (f'<div class="dfile"><div class="dfile-head"><span class="dpath">{e(c["file"])}</span>'
            f'<span class="dtag">lines {a}–{b} at fault, on main</span></div>'
            + (f'<div class="dscroll"><table class="dtable blob">{rows}</table></div>' if rows else
               '<p class="gh-muted gh-pad">The file is not on this host any more.</p>') + '</div>')
    why = (f'<div class="gh-annot muted"><b>Why this is the cause</b>{_md(c.get("why", ""))}'
           + (f'<p><span class="gh-muted">Plan for the fix:</span> {e(c["plan"])}</p>' if c.get("plan") else "")
           + (f'<p><span class="gh-muted">Code it looked up:</span> {e(", ".join(c["looked_up"]))}</p>' if c.get("looked_up") else "")
           + '</div>')
    files = diffview.parse(fix_patch or "")
    change = (diffview.html(fix_patch) if files else
              '<p class="gh-muted">No change yet: Fix it writes it next.</p>' if not (s.get("fix") or {}).get("status") else
              '<p class="gh-muted">No fix passed its tests, so there is no change to show.</p>')
    meta = (f'<span class="Label Label--done">AI-generated</span><b>debugassist</b> found the cause in '
            f'<code class="gh-ref">{e(Path(c["file"]).name)}</code>, lines {a}–{b}')
    panes = (f'{ai}<div class="pr-pane" data-pane="cause">{why}{code}</div>'
             f'<div class="pr-pane" data-pane="change" hidden>{change}</div>')
    return _sheet("codechange", "Code change", "Code change", meta, panes,
                  [("cause", "Cause", None), ("change", "Files changed", len(files) or None)])


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


def report_sheet(s: dict, ai: str = "") -> str:
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
        body = (f'<div class="gh-annot muted"><b>Summary</b>{_md(first)}'
                + (f'<p><span class="gh-muted">The condition that let it ship:</span> {e(cond)}</p>' if cond else "") + '</div>'
                + "".join(f'<details class="gh-fold"{" open" if i == 0 else ""}><summary>{e(q)}</summary>{_md(a)}</details>'
                          for i, (q, a) in enumerate(qa))
                + '<p class="gh-muted gh-fp">It describes conditions in the code and the process, never people.</p>')
    issue = s.get("issue") or {}
    meta = ('<span class="Label Label--done">AI-generated</span><b>debugassist</b> wrote this report from the history of '
            'the code at fault, its review and release, and the issue')
    return _sheet("report", "Why the bug slipped through", f'Why the bug slipped through <span class="gh-muted">#{e(issue.get("number", ""))}</span>',
                  meta, ai + body)


def guard_sheet(s: dict, ai: str = "") -> str:
    """Guard similar bugs, in plain words (Isha 2026-10-09: "I don't know exactly what happens here")."""
    g = s.get("guard") or {}
    of = g.get("on_fixed")
    if of is None:
        return ""
    passed, opened, broken = of.get("passed") or [], of.get("failed") or [], of.get("broken") or []
    total = len(passed) + len(opened) + len(broken)
    def rows(names, dot, word, why):
        return "".join(f'<div class="gh-check"><span class="gh-dot {dot}"></span><span class="gh-cname"><b>{e(str(n)[:200])}</b>'
                       f'<span class="gh-muted">{why}</span></span><span class="gh-st {dot}">{word}</span></div>' for n in names)
    sib = g.get("siblings") or []
    controls = g.get("unfixed_passed") or []
    on_old = (f"{total - len(controls)} of {total} cases failed on the old code, so the test would have caught this bug; "
              f"the other {len(controls)} pass there too, on purpose: they check that input that already worked keeps working."
              if controls else "Every case failed on the old code, so the test would have caught this bug.")
    body = (f'<div class="gh-annot muted"><b>What it is</b><p>One extra test with {total} cases. Each case is a different way '
            f'this kind of bug can happen. {e(on_old)} On the '
            f'fixed code, a passing case is covered by the fix; a failing one is a gap the fix leaves open.</p>'
            f'<p><span class="gh-muted">What it checks:</span> {e(g.get("covers") or g.get("text") or "")}</p>'
            + (f'<p><span class="gh-muted">A failing case counts when its failure shows one of:</span> '
               f'{", ".join(f"<code>{e(t)}</code>" for t in g["checked_against"])} (from the issue and the test that showed the bug)</p>'
               if g.get("checked_against") else "")
            + ('<p><span class="gh-muted">The issue quotes no code, so cases were judged by their failing assertion.</span></p>'
               if g.get("symptom_checked") is False else "") + '</div>'
            f'<div class="gh-box"><div class="gh-box-head"><span>Test cases on the fixed code</span>'
            f'<span class="gh-muted">{len(passed)} passing · {len(opened)} failing · {len(broken)} errors</span></div>'
            f'{rows(opened, "bad", "Failing", "still open: the fix does not cover it")}{rows(broken, "warn", "Error", "the case did not run")}'
            f'{rows(passed, "ok", "Passing", "covered by the fix")}</div>'
            + (f'<div class="gh-box"><div class="gh-box-head"><span>The same code in {len(sib)} other file{"s" * (len(sib) != 1)}, not changed by this fix</span></div>'
               + "".join(f'<div class="gh-check"><span class="gh-cname"><code>{e(x)}</code></span></div>' for x in sib) + '</div>' if sib else "")
            + (f'<p class="gh-muted gh-fp">File <code>{e(g.get("repo_path", ""))}</code> · kept with the run, not added to the pull request.</p>'))
    meta = (f'<span class="Label Label--done">AI-generated</span><b>debugassist</b> wrote {total} test cases · '
            f'{len(passed)} passing on the fixed code' + (f', {len(opened)} failing' if opened else ""))
    return _sheet("guardinfo", "Guard similar bugs", "Guard for similar bugs", meta, ai + body)


def full_answer(raw: dict) -> str:
    """The advisors' server's whole answer, GitHub's way (Isha 2026-10-09): each part folded, its checks as GitHub
    check rows (passed or failed in words and colour, never a tick), the raw response last."""
    mr = raw.get("machine_result") or {}
    parts = [("Verdict", f'<p class="gh-pad"><b>{e(raw.get("verdict") or mr.get("state") or "none")}</b>'
                         + (f' · {e(mr.get("reason"))}' if mr.get("reason") else "")
                         + (f'<br>Needs: {e(", ".join(mr.get("needs") or []))}' if mr.get("needs") else "") + "</p>")]
    checks = mr.get("checks") or []
    if checks:
        parts.append(("Checks", "".join(
            f'<div class="gh-check"><span class="gh-dot {"ok" if c.get("passed") else "bad"}"></span><span class="gh-cname">'
            f'<b>{e(str(c.get("id", "")).replace("_", " "))}</b><span class="gh-muted">{e(c.get("detail", ""))}</span></span>'
            f'<span class="gh-st {"ok" if c.get("passed") else "bad"}">{"Passed" if c.get("passed") else "Failed"}</span></div>'
            for c in checks)))
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
            found.append(({"is_defect": "Likelihood of a defect", "confidence": "Confidence"}[k], str(mr[k])))
    if found:
        parts.append(("Findings", "".join(f'<div class="gh-check"><span class="gh-cname"><b>{e(a)}</b><span class="gh-muted">{e(b)}</span>'
                                          f'</span></div>' for a, b in found)))
    if mr.get("rules"):
        parts.append(("Rules", '<ul class="gh-list">' + "".join(f"<li>{e(x)}</li>" for x in mr["rules"]) + "</ul>"))
    ft = raw.get("falsifiable_test") or {}
    if ft:
        parts.append(("How this answer will be tested", f'<p class="gh-pad">{e(ft.get("statement", ""))}<br><span class="gh-muted">'
                                                        f'{e(ft.get("resolve_rule", ""))} · now: {e(ft.get("state", ""))} · test '
                                                        f'{e(ft.get("test_id", ""))}</span></p>'))
    cal = raw.get("calibration") or {}
    if cal:
        parts.append(("Track record", f'<p class="gh-pad">{e(cal.get("judgments", 0))} answers, {e(cal.get("resolved", 0))} checked '
                                      f'against what happened ({e(cal.get("passed", 0))} right, {e(cal.get("failed", 0))} wrong). '
                                      f'{e(cal.get("pass_rate_note") or "")}</p>'))
    parts.append(("Raw response (JSON)", f'<pre class="gh-pre">{e(json.dumps(raw, indent=1, default=str)[:16000])}</pre>'))
    ref = raw.get("judgment_id")
    return ('<div class="fa gh">' + (f'<p class="gh-muted gh-fp">Reference <code class="gh-ref">{e(ref)}</code> · seat {e(raw.get("seat", ""))}</p>' if ref else "")
            + "".join(f'<details class="gh-fold"{" open" if i < 2 else ""}><summary>{e(t)}</summary>{body}</details>'
                      for i, (t, body) in enumerate(parts)) + "</div>")


def _advisors(s: dict) -> str:
    """Where an advisor seat reviews a step's output, and what happened, in plain words: agrees, or whose plan you chose."""
    from .advisors import ASK, REVIEWS, compare, status
    now, done, rows, sheets = status()[0], s.get("advisors") or {}, [], []
    said = {"OFF": "Not asked: advisors are off (not connected yet)",
            "BLOCKED": "Not asked: the advisors' server has not been reviewed yet",
            "ON": "Will be asked when this step finishes", "FAILED": "Could not be reached; the run went on without it"}
    label = {True: '<span class="Label Label--done">Agrees</span>', False: '<span class="Label Label--attention">Disagrees</span>',
             None: '<span class="Label">Can\'t compare</span>'}
    step_name = dict((k, v) for k, v, _ in plain.STEPS)
    for step, r in REVIEWS.items():
        rec = done.get(step) or {}
        st = rec.get("status", now)
        role = (ASK.get(r["seat"]) or {}).get("role", r["seat"])
        # Isha 2026-10-10: say what it was asked, what it said and whether it agrees with the run, in plain words
        answer = re.sub(r"\s*Reference judg_\w+\.?", "", rec.get("answer", ""))
        answer = re.sub(r"\s*Its fix verdict reads FAIL only because.*?judgment of the fix\.", "", answer)
        cmp = compare(step, rec.get("raw") or {}, s) if st == "ANSWERED" else None
        chose = (s.get("advisor_choices") or {}).get(step) or {}
        then = ("So the run went on by itself." if cmp and cmp["agrees"] is not False else
                "You chose the advisor's plan." if chose.get("choice") == "advisor" else
                "You kept the run's plan." if chose.get("choice") == "run" else
                "The run waits for your choice before the next step.")
        sub = (f'<span>Asked: {e(r["question"].split("?")[0] + "?")}</span><span>Said: {e(answer[:240])}</span>'
               f'<span>{label[cmp["agrees"]]} {e(cmp["why"][:1].upper() + cmp["why"][1:])}. {e(then)}</span>'
               if cmp else f"<span>{e(said.get(st, 'Not asked'))}</span>")
        ic = icons.check() if st == "ANSWERED" else icons.pause() if st in ("OFF", "BLOCKED") else icons.cross() if st == "FAILED" else icons.list_(18)
        raw, pid = rec.get("raw"), f"advfull-{step}"
        btn = (f'<button type="button" class="tbtn proof-btn" popovertarget="{pid}">{icons.list_(14)}<span>Full answer</span></button>'
               if raw else "")
        ic = _seal(r["seat"]) if st == "ANSWERED" else ic   # its seal, in its colour
        rows.append(f'<li class="row adv-row {"done" if st == "ANSWERED" else "pending"}"><span class="ic">{ic}</span><span class="t adv">'
                    f'<b>{e(role)} advisor ({e(r["seat"])}) · after {e(step_name.get(step, step))}</b>{sub}</span>'
                    f'<span class="tr">{btn}</span></li>')
        if raw:
            verdict = raw.get("verdict") or (raw.get("machine_result") or {}).get("state") or ""
            sheets.append(_sheet(pid, f'{r["seat"]}: full answer', f'{e(r["seat"])} review',
                                 f'{_seal(r["seat"])}<span class="Label Label--accent">{e(_who(r["seat"])["role"])}</span><b>{e(r["seat"])}</b> reviewed {e(r["what"])} '
                                 f'· via the Domain Expertise MCP server' + (f' · verdict <code class="gh-ref">{e(verdict)}</code>' if verdict else ""),
                                 full_answer(raw)))
    return (f'<section class="group"><h2>Advisors</h2><div class="sect"><ul class="rows">{"".join(rows)}</ul></div>{"".join(sheets)}'
            '<p class="foot">How they connect: after these four steps, DebugAssistAgent sends the step\'s result to the Domain '
            'Expertise MCP server. Each advisor there is a fixed rule check, not an AI, and answers in seconds. The answer '
            'is shown here and logged. When one disagrees with what the run did, the run pauses before the next step and '
            'you choose whose plan it follows; when it agrees, the run goes on by itself. '
            '<a href="/connect#advisors">How to connect them</a>.</p></section>')


def _engineer_details(d: dict, s: dict, rows: list[dict], live: bool) -> str:
    """What an engineer needs to audit the run beyond the steps and Activity above: the ladder, the fix attempts, the
    guard, the back-test and the spend. Folded away from the operator."""
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
    # Isha 2026-10-10: no step log or event log here; the steps and the Activity menu at the top already show them
    spend = f'''<div class="card"><h2>Spend, call by call</h2><div class="scroll"><table><tr><th>at</th><th>step</th><th>model</th><th>in</th><th>out</th><th>cost</th><th>status</th></tr>{calls}</table></div>{trials}</div>'''
    return (f'<details class="eng"><summary>Details for engineers</summary><div class="eng-body">'
            f'{_k("work", work)}{_k("lessons", lessons)}'
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
document.addEventListener("click", async ev => {   // install a missing package? (a second tap confirms)
  const b = ev.target.closest("[data-install]"); if (!b) return;
  const box = b.closest(".decide"), msg = box.querySelector(".decide-msg");
  if (b.dataset.armed !== "1") {
    box.querySelectorAll("[data-install]").forEach(x => { x.dataset.armed = ""; x.querySelector("span").textContent = x.dataset.label; });
    b.dataset.armed = "1"; b.querySelector("span").textContent = b.dataset.confirm;
    setTimeout(() => { if (b.dataset.armed === "1") { b.dataset.armed = ""; b.querySelector("span").textContent = b.dataset.label; } }, 5000);
    return;
  }
  box.querySelectorAll("[data-install]").forEach(x => { x.disabled = true; });
  msg.textContent = b.dataset.busy || (b.dataset.install === "yes" ? "Installing…" : "Stopping…");
  try {
    const r = await fetch(box.dataset.api || "/api/install", { method: "POST", headers: { "X-DebugAssistAgent-Token": TOKEN, "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: box.dataset.run, answer: b.dataset.install }) });
    const j = await r.json();
    msg.textContent = r.ok ? j.said : (j.error || "Something went wrong.");
    if (!r.ok) box.querySelectorAll("[data-install]").forEach(x => { x.disabled = false; });
  } catch (e) { msg.textContent = "Could not reach DebugAssistAgent."; box.querySelectorAll("[data-install]").forEach(x => { x.disabled = false; }); }
});
addEventListener("load", () => { for (const id of ["installask", "advisorask"]) { const a = document.getElementById(id); if (a && a.showPopover) { try { a.showPopover(); } catch (e) {} } } });
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
