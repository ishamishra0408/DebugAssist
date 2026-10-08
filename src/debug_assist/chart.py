"""The state chart: how a run moves, drawn as states (boxes, nouns) joined by actions (arrows, verbs). Amber arrows are
retries; red arrows end the run with a plain stop reason. A dot walks the path: "This run" walks the run's real path
(from its state and event log), "Every path" walks a scripted tour that shows each retry once. Pattern from the
delivery state machine Isha shared (2026-10-07); drawn in DebugAssistAgent's own style.
"""
import json

from . import plain

# states down the middle: (id, label)
STATES = [("issue", "Issue received"), ("triaged", "Triaged as a bug"), ("context", "Context gathered"),
          ("shown", "Bug shown"), ("cause", "Cause named"), ("fixed", "Fix passes its test"),
          ("proven", "Fix proven by 2 tests"), ("why", "Why it slipped: explained"), ("guard", "Guard written"),
          ("old", "Old code checked"), ("wait", "Waiting for your OK"), ("ready", "PR text ready")]
# the action on each arrow down the spine
ACTIONS = ["read and triage", "gather comments, code, history", "write a test that fails", "find the cause",
           "write a fix", "check with a second test, written blind", "trace the history",
           "write a check for similar bugs", "run it on older versions", "lock the pull request text", "you approve"]
# where a run can stop: from state → plain reason
STOPS = {"issue": "Stopped: not a bug, or a person must read it", "triaged": "Stopped: no code matches the issue",
         "context": "Stopped: bug never shown (4 tries)", "shown": "Stopped: cause not found",
         "cause": "Stopped: no fix passed (3 tries)", "proven": "Stopped: could not explain why it slipped",
         "why": "Stopped: no check for similar bugs", "wait": "Stopped: you said no"}
# retries: (id, from state, to state, label)
LOOPS = [("try", "context", "context", "test failed for another reason: try again"),
         ("refix", "cause", "cause", "fix failed the tests: try again"),
         ("round2", "fixed", "cause", "second test failed: write the fix again")]
EXIT_FROM = {"NEEDS PERSON": "issue", "NOT A DEFECT": "issue", "CONTEXT NOT FOUND": "triaged",
             "NEVER REPRODUCED": "context", "CAUSE NOT FOUND": "shown", "FIX NOT VALIDATED": "cause",
             "TEST FLAWED": "cause", "STORY NOT WRITTEN": "proven", "GUARD NOT WRITTEN": "why", "REJECTED": "wait"}

W, X0, BW, BH, GAP, TOP = 1000, 330, 250, 52, 100, 28  # canvas width, box x, box w/h, vertical pitch, top
PX, PW = 680, 300                                         # stop pills: x and width


def _y(i: int) -> int:
    return TOP + i * GAP


def _e(t: str) -> str:
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


ADVISED = {"triaged": "read_issue", "cause": "find_cause", "why": "why_it_shipped", "guard": "lasting_guard"}  # chart state → reviewed step
ADV_WORD = {"OFF": "off", "BLOCKED": "not reviewed yet", "ON": "on", "ANSWERED": "answered", "FAILED": "unreachable"}


def advisor_states(d: dict | None) -> dict:
    """Per reviewed step: what its seat did in this run, or (not reached yet / no run) what the settings say."""
    from .advisors import status
    now = status()[0]
    done = ((d or {}).get("state") or {}).get("advisors") or {}
    return {step: (done.get(step) or {}).get("status", now) for step in ADVISED.values()}


def svg(adv: dict | None = None) -> str:
    from .advisors import REVIEWS
    adv = adv or {}
    idx = {s: i for i, (s, _) in enumerate(STATES)}
    cx = X0 + BW / 2
    parts = ['<defs>'
             '<marker id="cG" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" class="mk g"/></marker>'
             '<marker id="cA" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" class="mk a"/></marker>'
             '<marker id="cR" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" class="mk r"/></marker>'
             '</defs>']
    # spine: one arrow per action
    for i, act in enumerate(ACTIONS):
        y1, y2 = _y(i) + BH, _y(i + 1)
        parts.append(f'<path class="ce" id="ce-{STATES[i][0]}" d="M{cx} {y1 + 2} L{cx} {y2 - 4}" marker-end="url(#cG)"/>')
        parts.append(f'<text class="cl" x="{cx + 12}" y="{(y1 + y2) / 2 + 4}">{_e(act)}</text>')
    # retries on the left
    for lid, a, b, label in LOOPS:
        ya, yb = _y(idx[a]) + BH / 2, _y(idx[b]) + BH / 2
        if a == b:
            d = f"M{X0} {ya + 10} C{X0 - 90} {ya + 46}, {X0 - 90} {ya - 46}, {X0 - 4} {ya - 10}"
            ty = ya + 4
        else:
            d = f"M{X0} {ya} C{X0 - 140} {ya}, {X0 - 140} {yb}, {X0 - 4} {yb}"
            ty = (ya + yb) / 2 + 4
        parts.append(f'<path class="ce warn" id="ce-{lid}" d="{d}" marker-end="url(#cA)"/>')
        parts.append(f'<text class="cl warn" x="{X0 - 104 if a == b else X0 - 128}" y="{ty}" text-anchor="end">'
                     + "".join(f'<tspan x="{X0 - 104 if a == b else X0 - 128}" dy="{0 if k == 0 else 15}">{_e(w)}</tspan>'
                               for k, w in enumerate(label.split(": ")))
                     + '</text>')
    # stops on the right
    for s, reason in STOPS.items():
        y = _y(idx[s]) + BH / 2
        parts.append(f'<path class="ce bad" id="ce-stop-{s}" d="M{X0 + BW} {y} L{PX - 4} {y}" marker-end="url(#cR)"/>')
        parts.append(f'<g class="cpill" id="cp-{s}"><rect x="{PX}" y="{y - 17}" width="{PW}" height="34" rx="17"/>'
                     f'<text x="{PX + PW / 2}" y="{y + 4}" text-anchor="middle">{_e(reason)}</text></g>')
    # advisors: a seat beside the step it reviews (advice only; dotted, because it never changes the run)
    for s, step in ADVISED.items():
        y, st = _y(idx[s]) + BH / 2, adv.get(step, "OFF")
        seat = REVIEWS[step]["seat"]
        parts.append(f'<path class="ce adv-link" d="M{X0 - 44} {y} L{X0 - 4} {y}"/>')
        parts.append(f'<g class="cadv {st.lower()}" id="ca-{s}"><rect x="{X0 - 294}" y="{y - 17}" width="250" height="34" rx="17"/>'
                     f'<text x="{X0 - 169}" y="{y + 4}" text-anchor="middle">Advisor {_e(seat)}: {_e(ADV_WORD.get(st, st.lower()))}</text></g>')
    # states
    for i, (s, label) in enumerate(STATES):
        y = _y(i)
        parts.append(f'<g class="cn" id="cn-{s}"><rect x="{X0}" y="{y}" width="{BW}" height="{BH}" rx="14"/>'
                     f'<text x="{cx}" y="{y + BH / 2 + 5}" text-anchor="middle">{_e(label)}</text></g>')
    parts.append('<circle class="ctok" r="8" cx="-20" cy="-20"/>')
    h = _y(len(STATES) - 1) + BH + TOP
    return (f'<svg class="chart-svg" viewBox="0 0 {W} {h}" role="img" aria-label="How a run moves: states and the '
            f'actions between them">{"".join(parts)}</svg>')


# ── paths the dot walks: [edge id, state it lands on, caption] ──────────────────────────────────
def every_path() -> list:
    sp = [f"ce-{s}" for s, _ in STATES]
    return [[sp[0], "triaged", "It reads the issue and decides it is a real bug."],
            [sp[1], "context", "It gathers the comments, the code around the problem and its recent changes."],
            ["ce-try", "context", "Its first test fails for another reason, so it tries again (up to 4 tries)."],
            [sp[2], "shown", "A test now fails because of the bug: the bug is shown."],
            [sp[3], "cause", "It names the file and lines that cause the bug."],
            ["ce-refix", "cause", "Its first fix fails the tests. The change is undone and it tries again (up to 3)."],
            [sp[4], "fixed", "A fix passes the test that shows the bug."],
            ["ce-round2", "cause", "A second test, written without seeing the fix, fails. It writes the fix again."],
            [sp[4], "fixed", "The new fix passes."],
            [sp[5], "proven", "A fresh third test passes too: the fix is proven by 2 tests."],
            [sp[6], "why", "It traces the history to explain how the bug got past review."],
            [sp[7], "guard", "It writes a check that catches bugs like this one."],
            [sp[8], "old", "It runs that check on older versions of the code."],
            [sp[9], "wait", "It locks the pull request text and waits for you."],
            [sp[10], "ready", "You approve. The pull request text is ready. Nothing is posted to GitHub."]]


def this_run(d: dict) -> list:
    """The run's real path, from its state and event log. Stops where the run is now."""
    s, evs = d["state"], d["events"]
    outcome = s.get("outcome") or {}
    sp = {a: f"ce-{a}" for a, _ in STATES}
    steps, at = [], "issue"

    def stop(frm: str):
        steps.append([f"ce-stop-{frm}", f"stop-{frm}", plain.exit_text(outcome.get("exit"))])

    if not s.get("triage"):
        return steps
    if EXIT_FROM.get(outcome.get("exit")) == "issue":
        stop("issue"); return steps
    steps.append([sp["issue"], "triaged", plain.result("read_issue", s) or "Read the issue."]); at = "triaged"
    if s.get("context") and s["context"].get("status") == "GATHERED":
        steps.append([sp["triaged"], "context", plain.result("gather_context", s)])
    elif outcome.get("exit") == "CONTEXT NOT FOUND":
        stop("triaged"); return steps
    elif s.get("repro") or s.get("attempts"):
        steps.append([sp["triaged"], "context", "This run had no Gather context step (it was added later)."])
    else:
        return steps
    tries = [a for a in s.get("attempts", []) if a.get("step") == "reproduce"]
    first_red = next((i for i, a in enumerate(tries) if a.get("outcome") == "RED"), None)
    for a in tries[:first_red if first_red is not None else len(tries)]:
        steps.append(["ce-try", "context", plain.happened({"kind": "attempt", **a}) + ". Trying again."])
    if outcome.get("exit") == "NEVER REPRODUCED":
        stop("context"); return steps
    if (s.get("repro") or {}).get("status") != "REPRODUCED":
        return steps
    steps.append([sp["context"], "shown", plain.result("reproduce", s)])
    if outcome.get("exit") == "CAUSE NOT FOUND":
        stop("shown"); return steps
    if not s.get("cause") or s["cause"].get("status") == "PLACEHOLDER":
        return steps
    steps.append([sp["shown"], "cause", plain.result("find_cause", s) or "Cause named."])
    fix = s.get("fix") or {}
    fa = [x for x in evs if x.get("kind") == "fix_attempt"] or fix.get("attempts") or []
    for x in fa:
        if x.get("ok"):
            break
        steps.append(["ce-refix", "cause", f"Fix try {x.get('n')} did not pass. The change is undone; trying again."])
    if outcome.get("exit") in ("FIX NOT VALIDATED", "TEST FLAWED"):
        stop("cause"); return steps
    if fix.get("status") != "VALIDATED":
        return steps
    steps.append([sp["cause"], "fixed", "A fix passes the test that shows the bug."])
    ho = fix.get("holdout") or {}
    if "round 2" in str(ho.get("status", "")) or "SEEN" in str(ho.get("status", "")):
        steps.append(["ce-round2", "cause", "A second test, written without seeing the fix, failed. Writing the fix again."])
        steps.append([sp["cause"], "fixed", "The new fix passes."])
    judges = (s.get("fix_clock") or {}).get("judges")
    steps.append([sp["fixed"], "proven", "Proven by 2 tests." if judges == 2 else "Only 1 test passed: not proven, but it goes on."])
    if outcome.get("exit") == "STORY NOT WRITTEN":
        stop("proven"); return steps
    if not (s.get("second_story") or {}).get("text"):
        return steps
    steps.append([sp["proven"], "why", "It explained how the bug got past review."])
    if outcome.get("exit") == "GUARD NOT WRITTEN":
        stop("why"); return steps
    if not s.get("guard") or (s["guard"].get("status") == "PLACEHOLDER"):
        return steps
    steps.append([sp["why"], "guard", plain.result("lasting_guard", s) or "Check written."])
    if not s.get("backtest"):
        return steps
    steps.append([sp["guard"], "old", plain.result("test_past_bugs", s) or "Older versions checked."])
    if not (d.get("interrupt") or s.get("approval")):
        return steps
    steps.append([sp["old"], "wait", "The pull request text is locked. Waiting for your OK."])
    if outcome.get("exit") == "REJECTED":
        stop("wait"); return steps
    if (s.get("approval") or {}).get("status") == "APPROVED":
        steps.append([sp["wait"], "ready", "You approved. The pull request text is ready. Nothing is posted to GitHub."])
    return steps


def section(d: dict | None, mode: str = "run") -> str:
    """The chart with its two modes. `d` is a run (viewer.gather) or None for the general tour."""
    run_path = this_run(d) if d else []
    paths = {"run": run_path, "all": every_path()}
    start = "run" if (d and run_path) else "all"
    toggle = ('<div class="seg glass chart-modes" role="radiogroup" aria-label="Which path">'
              f'<label><input type="radio" name="cm" value="run"{" checked" if start == "run" else ""}>This run</label>'
              f'<label><input type="radio" name="cm" value="all"{" checked" if start == "all" else ""}>Every path</label></div>'
              if d and run_path else "")
    data = json.dumps(paths).replace("</", "<\\/")
    return (f'<div class="chart" data-paths="{_e(data)}" data-start="{start}">{toggle}'
            f'<div class="sect chart-box">{svg(advisor_states(d))}</div>'
            f'<p class="chart-cap" aria-live="polite">Starting…</p><p class="foot chart-hint">Click the chart to pause or resume.</p></div>')
