"""Every word the operator reads on DebugAssistAgent's pages, in plain English, in one place.

Rule (Isha, 2026-10-07): no fluff, no jargon. Say what happened and what to do. Engineer detail lives in the
"Details for engineers" section of the run page, never in these words.
"""

NAME = "DebugAssistAgent"

# The steps: (graph node, what the operator sees, what the step does)
STEPS = [("read_issue", "Read the issue", "Decides whether it is a real bug"),
         ("gather_context", "Gather context", "Reads the comments, the code around the problem and its recent changes"),
         ("reproduce", "Show the bug", "Writes a test that fails because of the bug"),
         ("find_cause", "Find the cause", "Finds the file and lines that cause it"),
         ("write_fix", "Fix it", "Writes a fix that passes 2 separate tests"),
         ("why_it_shipped", "Why it slipped", "Explains how the bug got past review"),
         ("lasting_guard", "Guard similar bugs", "Writes a check for bugs like this one"),
         ("test_past_bugs", "Check old code", "Runs that check on older versions"),
         ("approval", "Your OK", "Waits for you to approve"),
         ("open_pr", "PR ready", "Saves the pull request text for you")]
LABEL = {k: label for k, label, _ in STEPS}
INTRO = "(description)"   # the issue's opening text, before its first heading, as a section to prove (2026-10-10)

# How a run ended, in words an operator can act on
EXITS = {
    "READY FOR YOU TO PUBLISH": "Done. The pull request text is ready for you to send.",
    "NEEDS PERSON": "Stopped. A person needs to read this issue first.",
    "NOT A DEFECT": "Stopped. This does not look like a bug.",
    "CONTEXT NOT FOUND": "Stopped. It could not find any code that matches the issue.",
    "NEVER REPRODUCED": "Stopped. It could not make the bug happen, so it did not try to fix it.",
    "TEST MACHINE NOT READY": "Stopped. The test machine cannot run this part of the code's tests yet, so no test was "
                              "written and nothing was spent on one.",
    "CAUSE NOT FOUND": "Stopped. It could not find the code that causes the bug.",
    "FIX NOT VALIDATED": "Stopped. None of its fixes passed the tests.",
    "TEST FLAWED": "Stopped. The test that shows the bug turned out to be wrong.",
    "STORY NOT WRITTEN": "Stopped. It could not explain why the bug slipped through.",
    "GUARD NOT WRITTEN": "Stopped. It could not write a check for similar bugs.",
    "REJECTED": "Stopped. You said no to the pull request.",
}

TEST_KIND = {"unit": "quick test", "integration": "recorded-stream test", "end_to_end": "full test"}
TRY_RESULT = {"RED": "bug shown", "GREEN": "bug not shown", "ERROR": "test broke for another reason"}
OLD_CODE = {"CAUGHT": "check caught the bug", "MISSED": "check missed the bug", "FALSE ALARM": "false alarm",
            "QUIET": "no bug, no alarm", "UNEVALUABLE": "could not be tested"}


def exit_text(exit_name: str | None) -> str:
    return EXITS.get(exit_name or "", f"Stopped ({str(exit_name or 'unknown reason').lower()}).")


def happened(x: dict) -> str | None:
    """One thing that happened, in plain words, or None for events the operator doesn't need one by one."""
    k = x.get("kind")
    if k == "attempt":
        return f"Try {x.get('n')} ({TEST_KIND.get(x.get('rung'), x.get('rung'))}): {TRY_RESULT.get(x.get('outcome'), x.get('outcome'))}"
    if k == "fix_attempt":
        return f"Fix try {x.get('n')}: {'passed all tests' if x.get('ok') else 'did not pass'}"
    if k == "holdout":
        return "Second test: passes on the fix" if x.get("on_fixed") == "GREEN" else "Second test: fails on the fix"
    if k == "guard_try":
        return f"Check try {x.get('n')}: {'kept' if x.get('result') == 'accepted' else 'needs another try'}"
    if k == "backtest":
        return f"Old version {str(x.get('commit', ''))[:7]}: {OLD_CODE.get(x.get('state'), str(x.get('state')).lower())}"
    if k == "embed":
        return "Saved, so similar bugs can be found later"
    if k == "code" and x.get("key") == "rebuild":
        return "Rebuilding the test machine on the latest main: the package list changed since it was built"
    if k == "code" and x.get("error"):
        return "Could not read the latest main; using the saved copy of the code"
    if k == "code":
        n = len(x.get("build") or [])
        return (f"On main at {str(x.get('commit', ''))[:7]}, fetched just now"
                + (f"; rebuilt {n} changed package{'s' * (n != 1)}" if n else "")
                + ("; installed the new package list" if x.get("installed") else ""))
    if k == "install" and "seconds" in x:   # the rebuild finished (events come without their key)
        return f"Installed {x.get('package')} on the test machine"
    if k == "install":
        return f"You answered {x.get('answer')}: install {x.get('package')}"
    if k == "advisor":
        return f"Advisor {x.get('seat')}: " + {"OFF": "not asked (advisors are off)", "BLOCKED": "not asked (server not reviewed yet)",
                                               "ANSWERED": "answered", "FAILED": "could not be reached"}.get(x.get("status"), "not asked")
    if k == "context":
        return f"Read {x.get('comments', 0)} comments, {x.get('files', 0)} files, {x.get('changes', 0)} recent changes"
    if k == "approval":
        return "You approved" if x.get("status") == "APPROVED" else "You said no"
    if k == "laya":
        ans = x.get("answers") or {}
        if "is_defect" in ans:
            return "Triage: looks like a real bug" if ans["is_defect"].get("choice") in ("yes", True) else "Triage: may not be a bug"
        return "Triage done"
    return None


def activity(x: dict) -> str:
    """One line of the activity list."""
    k = x.get("kind")
    if k == "model_call":
        model = str(x.get("model", "")).split("/")[-1]
        cost = x.get("cost_usd", x.get("charged_usd")) or 0
        return f"Asked the AI ({model}), ${cost:.4f}" + ("" if x.get("ok", True) else ", it failed")
    if k == "sandbox":
        cmd, ok = str(x.get("command", "")), x.get("exit") == 0
        what = "Built the code" if " build" in cmd else "Installed packages" if " install" in cmd else "Ran tests"
        if what == "Ran tests":
            return f"Ran tests: {'all passed' if ok else 'some failed'} ({x.get('seconds')} s)"
        return f"{what}: {'done' if ok else 'failed'} ({x.get('seconds')} s)"
    if k == "step":
        ended = x.get("ended", "ok")
        tail = "" if ended == "ok" else " (waiting for you)" if ended == "paused for approval" else f" ({exit_text(ended)})"
        return f"Step finished in {x.get('seconds'):.0f} s{tail}" if isinstance(x.get("seconds"), (int, float)) else "Step finished"
    return happened(x) or k or "activity"


def counts(evs: list[dict]) -> str:
    calls = sum(x.get("kind") == "model_call" for x in evs)
    runs = sum(x.get("kind") == "sandbox" for x in evs)
    parts = [f"{calls} AI call{'s' * (calls != 1)}"] * bool(calls) + [f"{runs} test run{'s' * (runs != 1)}"] * bool(runs)
    return " · ".join(parts)


def duration(secs: float | None) -> str:
    if secs is None or secs < 0:
        return ""
    return f"{secs:.0f} s" if secs < 90 else f"{secs / 60:.0f} min" if secs < 5400 else f"{secs / 3600:.1f} h"


def result(key: str, s: dict) -> str | None:
    """What a finished step found, in one plain sentence, from the run's saved state. None when there is nothing to say."""
    from pathlib import Path
    if key == "read_issue" and isinstance((s.get("triage") or {}).get("is_defect_p"), (int, float)):
        p = s["triage"]["is_defect_p"]
        return f"Looks like a real bug ({p:.0%} sure)" if p >= .5 else f"May not be a bug (only {p:.0%} sure it is)"
    if key == "gather_context" and (c := (s.get("context") or {}).get("counts")):
        parts = [f"{c['comments']} comment{'s' * (c['comments'] != 1)}", f"{c['files']} files",
                 (f"{c['related']} piece{'s' * (c['related'] != 1)} of shared code" if c.get("related") else ""),
                 f"{c['changes']} recent changes"]
        return "Read " + ", ".join(p for p in parts if p)
    if key == "reproduce" and (r := s.get("repro") or {}).get("status") == "REPRODUCED":
        # the try that showed it, not the number of tries (2026-10-08: "try 3" when try 2 showed it and 3 confirmed it)
        n_of = {a.get("test_path"): a.get("n") for a in s.get("attempts") or []}
        shown, confirmed = n_of.get(r.get("failing_test")), n_of.get(r.get("oracle_test"))
        return (f"Bug shown on try {shown or r.get('attempts_used') or 1}"
                + (f", then confirmed on a recorded stream (try {confirmed})" if r.get("confirmed") and confirmed and confirmed != shown
                   else ", then confirmed on a recorded stream" if r.get("confirmed") else ""))
    if key == "find_cause" and (c := s.get("cause") or {}).get("file"):
        a, b = (c.get("lines") or [None, None])[:2]
        return f"In {Path(c['file']).name}" + (f", lines {a}–{b}" if a else "")
    if key == "write_fix" and (f := s.get("fix") or {}).get("status"):
        j = (s.get("fix_clock") or {}).get("judges")
        return ("Fix passes both tests" if f["status"] == "VALIDATED" and j == 2 else
                "Fix passes only 1 of 2 tests" if f["status"] == "VALIDATED" else "No fix passed the tests")
    if key == "why_it_shipped" and (s.get("second_story") or {}).get("text"):
        return "Report written"
    if key == "lasting_guard" and (g := s.get("guard") or {}).get("on_fixed") is not None:
        # Isha 2026-10-09: "New check with 66 cases -- I don't know exactly what happens here"
        of = g["on_fixed"]
        covered, still_open = len(of.get("passed", [])), len(of.get("failed", []))
        n = covered + still_open + len(of.get("broken", []))
        sib = len(g.get("siblings") or [])
        return (f"A {n}-case test for bugs like this one: {covered} covered by the fix"
                + (f", {still_open} still open" if still_open else "") + (f"; the same code is in {sib} other files" if sib else ""))
    if key == "test_past_bugs" and (b := (s.get("backtest") or {}).get("detail") or {}).get("false_alarms"):
        return f"Tested {b['false_alarms'].get('commits_covered', b['false_alarms'].get('window', '?'))} older versions"
    if key == "test_past_bugs" and (b := s.get("backtest")):
        # Isha 2026-10-09: "Saved, so similar bugs can be found later -- I don't know exactly what happens here"
        found = len(b.get("candidates") or [])
        return ("Saved how this bug slipped through, for finding similar bugs"
                + (f"; {found} similar past bug{'s' * (found != 1)} found" if found else "")
                + ". Older versions were not checked: that works on the Mac only for now")
    if key == "approval" and (a := s.get("approval") or {}).get("status"):
        return "You approved" if a["status"] == "APPROVED" else "You said no"
    if key == "open_pr" and s.get("published"):
        return "Pull request text saved. Nothing posted."
    return None
