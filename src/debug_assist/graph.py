"""The pipeline: 9 steps in a fixed order (Uber's fixed-plan idea), in three phases.

  Fix it       read_issue → reproduce → find_cause → write_fix          (⏱ fix clock: pickup → validated fix)
  Learn        why_it_shipped → lasting_guard → test_past_bugs          (🎯 would-have-caught)
  Ship         approval (pauses for your go-word) → open_pr (dry run: you publish)

STATUS (2026-10-07): every step is real except the back-test half of test_past_bugs (the search is real). Each one
gathers evidence in code, lets a model write only what code then checks, and stops with a named reason rather than
pass along anything unproven.

Typed early exits: a run can stop on purpose, with a named reason in `outcome`, instead of pushing on:
  NEEDS PERSON       triage confidence below the review bar, or the --focus-heading section is missing
  NOT A DEFECT       triage is confident it is not a bug
  NEVER REPRODUCED   the ladder used its attempts and nothing went red (no fix for a bug we could not see)
  CAUSE NOT FOUND / FIX NOT VALIDATED / TEST FLAWED   no proven fix (fixer.py)
  STORY NOT WRITTEN / GUARD NOT WRITTEN               no checked story or guard (story.py, guard.py)
Resume (Thursday): `debug-assist resume <run-id>` continues from the last finished step. Every step is safe to re-run:
the condition freeze and the stored condition are idempotent, spend and turns live in MongoDB (meter.py), and the
ladder reads the attempts it already made from the event log.
"""
import functools
import json
import operator
import re
import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.checkpoint.mongodb import MongoDBSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from . import advisors, backtest, context, diffview, events, fixer, guard, ladder, story, testwriter
from . import lang as langs
from .checkout import base_path, run_copy
from .config import CFG, REPRO_ATTEMPT_CAP
from .github_read import get_issue
from .issue_text import TRIAGE_QUESTIONS, clean
from .guardrails import (GuardrailViolation, assert_no_names, fingerprint, freeze_condition, now,
                         record_approval, verify_approval, verify_condition_frozen)
from .models import decide, embedder, write
from . import plain, profiles
from .profiles import PROFILES, profile_for
from .sandbox import secrets_visible
from .store import client, db

DB = db()
LEDGER = DB["ledger"]          # approvals and condition freezes (append-only by convention)
CONDITIONS = DB["conditions"]  # named conditions + embeddings, for finding past bugs of the same kind
VEC_INDEX = "conditions_vec"
NEEDS_PERSON, NOT_A_DEFECT = "NEEDS PERSON", "NOT A DEFECT"


class RunState(TypedDict, total=False):
    run_id: str
    issue_url: str
    issue: dict
    triage: dict
    fix_clock: dict
    repro: dict
    cause: dict
    fix: dict
    second_story: dict
    condition: dict
    guard: dict
    backtest: dict
    pr_body: str
    approval: dict
    published: dict
    spent_usd: float
    demo: bool
    trace: bool
    profile: dict
    preflight: list
    turns: dict
    focus: str          # the ONE problem in the issue this run reproduces (--focus, or a section via --focus-heading)
    focus_heading: str
    hints: dict         # the person's optional pointers: {"look_in": [files for the cause], "test_in": test file}
    code: dict          # the code this run is on: main's newest commit when it started (fresh.py), what the test
                        # machine was built at, and what moving to main needed (an install, or packages to build)
    context: dict       # Gather context: where the pack is, its sha256, the counts, and the brief later steps read
    advisors: Annotated[dict, lambda a, b: {**(a or {}), **(b or {})}]  # per step: what its seat said (advice only)
    attempts: Annotated[list, operator.add]  # append-only: every attempt and how it ended (also in the event log)
    outcome: dict                            # set when the run stops: {"exit": ..., "why": ..., "at": ...}
    log: Annotated[list, operator.add]


def step(fn):
    """Bind (run_id, step) while a step runs, so every model call, sandbox command and Laya decision inside it is
    metered and logged under this run."""
    @functools.wraps(fn)
    def bound(s: RunState):
        import time
        from langgraph.errors import GraphInterrupt
        from . import artifacts
        if artifacts.enabled():  # a host whose disk was wiped: put the run's files and code copies back first
            artifacts.restore_run(s["run_id"])
        with events.bind(s["run_id"], fn.__name__):
            t0 = time.monotonic()
            try:
                out = fn(s)
            except GraphInterrupt:  # the approval pause: time spent waiting for a person is not the step's own
                events.log("step", seconds=round(time.monotonic() - t0, 2), ended="paused for approval")
                raise
            except Exception as ex:
                events.log("step", seconds=round(time.monotonic() - t0, 2), ended=f"error: {type(ex).__name__}")
                raise
            finally:
                if artifacts.enabled():  # whatever happened, what the step wrote survives a restart
                    artifacts.save_run(s["run_id"])
            # every second of wall time is attributed to a step (de-advisor review 2026-10-07: model + sandbox time
            # explained only 58-67% of ⏱; fetch, Laya, copying the checkout and git made up the rest, unlogged)
            events.log("step", seconds=round(time.monotonic() - t0, 2), ended=(out or {}).get("outcome", {}).get("exit", "ok"))
            return out
    return bound


def stop(exit_: str, why: str) -> dict:
    return {"exit": exit_, "why": why, "at": now()}


def run_dir(s: RunState):
    d = CFG.runs_dir / s["run_id"]
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Phase 1: Fix it ─────────────────────────────────────────────────────────────────────────────
REPRO_Q = {  # not fine-tuned: answered by general Laya
    "has_repro": {"type": "noul", "instructions": "Does the issue include steps, code or a trace to reproduce it?"},
    "regression": {"type": "noul", "instructions": "Does it say it worked in an earlier version?"},
}


def read_issue(s: RunState):
    picked_up = now()  # ⏱ starts at pickup, before the fetch and triage (de-advisor review 2026-10-07)
    issue = get_issue(s["issue_url"])
    text = clean(issue["title"], issue["body"])  # the exact input format the triage model was trained on
    t = decide(text, TRIAGE_QUESTIONS, which="triage")
    g = decide(text, REPRO_Q, which="general")
    p = t["is_defect"]["noul"]
    triage = {"is_defect_p": p, "kind": t["kind"]["choice"], "kind_p": t["kind"]["answer_confidence"],
              "has_repro_p": g["has_repro"]["noul"], "regression_p": g["regression"]["noul"],
              "needs_person": max(p, 1 - p) < CFG.triage_review_below, "model": "laya-triage (fine-tuned)"}
    prof = profile_for(issue["owner"], issue["repo"])
    out = {"issue": issue, "triage": triage, "fix_clock": {"started_at": picked_up}, "spent_usd": 0.0, "turns": {},
           "profile": {"repo": prof.repo, "language": prof.language, "image": prof.image,
                       "recorded_fixtures": prof.recorded_fixtures},
           "log": [f"read_issue: {issue['repo']}#{issue['number']} triaged {triage}"]}
    focus = focus_of(issue, s.get("focus"), s.get("focus_heading"))
    out["focus"] = focus or issue["title"]
    if s.get("focus_heading") and not focus:
        out["outcome"] = stop(NEEDS_PERSON, f"the issue has no section headed '{s['focus_heading']}'; "
                                            "a person picks the problem to reproduce")
    elif triage["needs_person"]:
        out["outcome"] = stop(NEEDS_PERSON, f"triage confidence {max(p, 1 - p):.2f} is below the "
                                            f"{CFG.triage_review_below} review bar; a person reads the issue first")
    elif p < 0.5:
        out["outcome"] = stop(NOT_A_DEFECT, f"triage is confident this is not a bug (is_defect p={p:.2f})")
    if "outcome" in out:
        out["log"].append(f"read_issue: STOPPED {out['outcome']['exit']}: {out['outcome']['why']}")
    # defect-triage's own rule over the same numbers (confidence = how far p sits from 0.5): advice only
    out["advisors"] = {"read_issue": advisors.review(
        s, "read_issue", issue.get("body") or "", question=issue["title"],
        context={"repo_name": f"{issue['owner']}/{issue['repo']}"},
        numbers={"is_defect": p, "confidence": t["is_defect"].get("answer_confidence", max(p, 1 - p))})}
    return out


CONTEXT_NOT_FOUND = "CONTEXT NOT FOUND"


def gather_context(s: RunState):
    """One step, no AI: collect what the later steps read (context.py), save it with the run, lock it with a sha256."""
    prof = profiles.get(s["profile"]["repo"])
    code = s.get("code") or _fresh_code(s, prof)   # main's newest commit, and what moving to it needs (fresh.py)
    checkout = run_copy(prof, run_dir(s) / "checkout", code)  # this run's own unmodified code (reused as-is on resume)
    focus = s.get("focus") or s["issue"]["title"]
    try:
        pack = context.collect(s["issue"], focus, checkout, code.get("commit") or prof.base_commit, prof, s.get("hints"))
    except testwriter.WriterRefused as e:
        return {"code": code, "context": {"status": "NOT FOUND", "checkout": str(checkout), "why": str(e)},
                "outcome": stop(CONTEXT_NOT_FOUND, f"{e}; there is no code to show the bug in"),
                "log": [f"gather_context: STOPPED {CONTEXT_NOT_FOUND}: {e}"]}
    path = run_dir(s) / "context.json"
    sha = context.save(pack, path)
    c = pack["counts"]
    events.log("context", key="pack", sha256=sha[:12], **c, missing=pack["missing"], cut=pack["cut"])
    return {"code": code, "context": {"status": "GATHERED", "path": str(path), "sha256": sha, "checkout": str(checkout),
                        "located": pack["code"]["best"], "counts": c, "brief": pack["brief"],
                        "missing": pack["missing"], "cut": pack["cut"]},
            "log": [f"gather_context: {c['comments']} comments, {c['linked']} linked, {c['files']} files "
                    f"(best {pack['code']['best']}), {c['related']} shared definitions, {c['changes']} recent changes; "
                    f"brief {c['brief_chars']} chars; sha256 {sha[:12]}"
                    + (f"; missing: {', '.join(pack['missing'])}" if pack["missing"] else "")]}


def _fresh_code(s: RunState, prof) -> dict:
    """Every run on the latest main (Isha 2026-10-10). Read main's newest commit; make the run's copy at the test
    machine's commit and compare: a lockfile change rebuilds the machine at main (hosted) or installs into the copy (this
    Mac); otherwise only the changed packages and those using them are built. If main can't be read, the run says so and
    uses the pinned commit."""
    import dataclasses
    from . import fresh, pkgcheck
    from .checkout import on_commit
    try:
        top = fresh.latest(prof)
    except Exception as ex:
        events.log("code", key="main", commit=prof.base_commit, error=str(ex)[:300])
        return {"commit": "", "error": f"could not read the latest main ({str(ex)[:160]}); the saved copy was used"}
    built = fresh.machine(prof) if CFG.sandbox_backend == "e2b" else prof.base_commit
    copy = run_copy(prof, run_dir(s) / "checkout")          # at the saved copy; moved to main below
    fresh.move(copy, top["commit"], built)
    p = fresh.plan(prof, copy, built, top["commit"])
    code = {**top, "built_from": built, "files_changed": p["files"], "lock_changed": p["lock_changed"],
            "build": p["build"], "build_cmd": p["build_cmd"]}
    if p["lock_changed"] and CFG.sandbox_backend == "e2b":
        # the package list changed: the machine is rebuilt at main once, at the start of Show the bug, together with
        # any package that turns out to be missing (review of run #22543: rebuilt here, then again for @ai-sdk/vue)
        code.update(rebuild_pending=True)
    on_commit(prof, copy, code)
    events.log("code", key="main", commit=top["commit"], fetched_at=top["fetched_at"], built_from=code["built_from"],
               files_changed=p["files"], build=code["build"], rebuilt=bool(code.get("rebuilt")),
               installed=bool(code.get("lock_changed")))
    return code


def _ctx(s: RunState, checkout: Path):
    """The context every step reads: the saved pack when Gather context ran, else (older runs) a fresh locate."""
    return context.load_ctx(s) or testwriter.locate(checkout, s["issue"].get("body", ""), s.get("focus") or s["issue"]["title"],
                                                     profiles.get(s["profile"]["repo"]), s.get("hints"))


def focus_of(issue: dict, focus: str | None, heading: str | None) -> str:
    """The problem to reproduce: given outright, or the text under a markdown heading of the issue (e.g. #21439's
    "Secondary observation"). Empty when the heading isn't there."""
    if focus:
        return focus.strip()
    if not heading:
        return ""
    if heading == plain.INTRO:   # the issue's opening description, before its first heading
        import re
        return re.split(r"^#{1,6}[ \t]+", issue.get("body", ""), maxsplit=1, flags=re.M)[0].strip()
    import re
    m = re.search(rf"^#+\s*{re.escape(heading)}\s*$\n(.*?)(?=^#+\s|\Z)", issue.get("body", ""), re.M | re.S | re.I)
    return m.group(1).strip() if m else ""


def _write_and_run_test(s: RunState, rung: ladder.Rung, n: int, history: list, ctx, checkout) -> ladder.Attempt:
    """One rung attempt (testwriter.py): the writer drafts a test for this rung, seeing every earlier attempt and its
    evidence; the sandbox runs it with the network off; classify() and the right-reason check decide."""
    drafts = run_dir(s) / "writer"
    drafts.mkdir(exist_ok=True)
    return testwriter.attempt(s, rung, n, history, ctx, checkout, profiles.get(s["profile"]["repo"]), drafts=drafts,
                              proof_dir=run_dir(s) / "proof")


def _existing_tests(s: RunState, checkout: Path, ctx, prof) -> dict:
    """Does a test already in the repo fail for this issue? The package's own tests, once, on the unfixed code, with
    the internet off (Isha 2026-10-08: show that first; only when none does is a test written). Recorded as an event,
    so a resumed run does not run them again."""
    done = next((e for e in events.for_run(s["run_id"]) if e["kind"] == "existing_tests"), None)
    if done:
        return {k: v for k, v in done.items() if k not in ("run_id", "step", "kind", "at", "key")}
    from .sandbox import run_in_sandbox
    lang = langs.of(prof)
    pkg = getattr(ctx, "package_dir", "") or lang.package_of(ctx.source)
    focus = s.get("focus") or s["issue"]["title"]
    try:
        cmd = lang.test_command(pkg, where=checkout)
    except ValueError as ex:
        return {"status": "NOT CHECKED", "why": str(ex), "package": pkg}
    r = run_in_sandbox(cmd, checkout, network=False, timeout=600, image=prof.image)
    out = (r.stdout or "") + (r.stderr or "")
    blocks = lang.failure_blocks(out)
    for_issue = [h[:200] for h, b in blocks.items() if testwriter.right_reason(focus, b)]
    # review of run #22085 (2026-10-09): the issue quoted no code, so nothing could say whether a failure is this bug's
    cant_tell = bool(blocks) and not testwriter.check_terms(focus)
    # none of the package's own tests could even load (#22085 run, 2026-10-08: @ai-sdk/workflow was not installed on the
    # test machine, every file failed on a missing config, and 4 tries were then spent on tests that could never run)
    cannot = (r.returncode not in (0, 124, 125) and not testwriter.counts(out).get("passed")
              and ladder.classify(prof.language, r.returncode, out)[0] == ladder.ERROR)
    status = ("FOUND" if for_issue else "NONE FAIL" if r.returncode == 0 else
              "NOT CHECKED" if r.returncode in (124, 125) else "CANNOT RUN" if cannot else
              "CAN'T TELL" if cant_tell else "OTHER FAILURES")
    res = {"status": status, "package": pkg, "exit": r.returncode, **testwriter.counts(out), "for_issue": for_issue[:5],
           **({"why": ladder.classify(prof.language, r.returncode, out)[1][:300]} if cannot else {}),
           "other_failures": [h[:200] for h in blocks if h[:200] not in for_issue][:5],
           "proof": str(testwriter.write_proof(run_dir(s) / "proof", f"{pkg} (the repo's own tests)", None, cmd,
                                               r.returncode, out, status, "", prof, checkout, name="existing-tests"))}
    events.log("existing_tests", key="suite", **res)
    return res


def _after_fix(s: RunState, prof, checkout: Path, paths: list) -> list[dict]:
    """Each test that showed the bug, run again on the FIXED code: the proof that it now passes. A test that was moved
    out of the code (attempt-tests/) is put back for the run and taken out again."""
    from .sandbox import run_in_sandbox
    lang, out = langs.of(prof), []
    for rel in dict.fromkeys(p for p in paths if p):
        dest, put_back = checkout / rel, False
        if not dest.exists():
            shelved = run_dir(s) / "attempt-tests" / Path(rel).name
            if not shelved.exists():
                continue
            dest.write_text(shelved.read_text())
            put_back = True
        try:
            cmd = lang.test_command(lang.package_of(rel), rel, where=checkout)
            r = run_in_sandbox(cmd, checkout, network=False, timeout=300, image=prof.image)
            text = (r.stdout or "") + (r.stderr or "")
            outcome, line = ladder.classify(prof.language, r.returncode, text)
            changed = [p for p in diffview.changed_paths(checkout) if not diffview.is_test(p)]
            line = line.replace("on the unfixed code", "with the fix")
            proof = testwriter.write_proof(run_dir(s) / "proof", rel, dest.read_text(), cmd, r.returncode, text, outcome,
                                           line, prof, checkout, name=f"after-fix-{Path(rel).name}", fixed=changed or ["source"])
            out.append({"test_path": rel, "outcome": outcome, "line": line, "proof": str(proof)})
            events.log("after_fix", key=rel, test=rel, outcome=outcome)
        finally:
            if put_back:
                dest.unlink(missing_ok=True)
    return out


TEST_MACHINE_NOT_READY = "TEST MACHINE NOT READY"


def _install_if_missing(s: RunState, checkout: Path, ctx, plan: dict, seen) -> dict | None:
    """Before any test is written: is the package the bug lives in installed on the test machine? If not, the run
    pauses and asks (Isha 2026-10-10). Yes: rebuild the test machine with it, then add it to the repo's install list for
    good, and go on. No: stop, nothing spent. None = installed (or nothing to check); else the step's stopped output."""
    import dataclasses
    import time
    from . import pkgcheck
    prof = profiles.get(s["profile"]["repo"])
    pkg = getattr(ctx, "package_dir", "") or langs.of(prof).package_of(ctx.source)
    gap = pkgcheck.missing(prof, checkout, pkg)
    if not gap:
        if (s.get("code") or {}).get("rebuild_pending"):
            return _rebuild_at_main(s, prof, checkout, plan, seen)
        return None
    answer = interrupt({"kind": "install", "package": gap["name"], "dir": gap["dir"], "repo": prof.repo,
                       "minutes": pkgcheck.MINUTES})
    def stopped(why: str) -> dict:
        return {"repro": {"status": "NOT RUN", "ladder_plan": plan, "attempts_used": 0, "failing_test": None,
                          "oracle_test": None, "located": ctx.source, "checkout": str(checkout),
                          "sandbox_secrets_visible": seen, "install": {"package": gap["name"], "answer": str(answer)}},
                "outcome": stop(TEST_MACHINE_NOT_READY, f"{why}; no test was written"),
                "log": [f"reproduce: STOPPED {TEST_MACHINE_NOT_READY}: {why}"]}
    if str(answer).strip().lower() != "yes":
        events.log("install", key=f"{gap['name']}-no", package=gap["name"], answer="no")
        return stopped(f"{gap['name']} is not installed on the test machine, and you chose not to install it")
    events.log("install", key=f"{gap['name']}-yes", package=gap["name"], answer="yes")
    code = s.get("code") or {}
    pending = code.get("rebuild_pending")   # hosted, the package list changed too: one rebuild, at main
    machine_at = code["commit"] if pending else code.get("built_from") or prof.base_commit
    plus = dataclasses.replace(prof, filters=(prof.filters + f" --filter '{gap['name']}...'").strip(), base_commit=machine_at)
    t0 = time.monotonic()
    try:
        pkgcheck.rebuild(plus, checkout)
    except Exception as ex:
        return stopped(f"installing {gap['name']} failed: {str(ex)[:300]}")
    events.log("install", key=f"{gap['name']}-built", package=gap["name"], seconds=round(time.monotonic() - t0))
    if pending:
        _machine_at_main(s, prof, checkout)
    if pkgcheck.missing(plus, checkout, pkg, trust_list=False):
        return stopped(f"{gap['name']} is still not installed after the rebuild")
    pkgcheck.add(prof.repo, gap["name"])   # for good: later runs of this repo never ask again
    return None


def time_away(evs: list[dict], start: str, stop_: str) -> dict:
    """Time inside the fix clock that is not the pipeline fixing (review of run #22543: 62 s waiting for the install
    answer and 149 s of installing were counted): waiting for a person's answer, and installing packages."""
    t = datetime.fromisoformat
    mine = [x for x in evs if start <= x["at"] <= stop_]
    waiting = 0.0
    for i, x in enumerate(mine):
        if x.get("kind") == "step" and x.get("ended") == "paused for approval":
            answer = next((y for y in mine[i + 1:] if y.get("kind") in ("decision", "install") and
                           (y.get("answer") or y.get("decision"))), None)
            waiting += (t(answer["at"]) - t(x["at"])).total_seconds() if answer else 0.0
    installing = sum(float(x.get("seconds") or 0) for x in mine
                     if (x.get("kind") == "install" and "answer" not in x) or (x.get("kind") == "prepare" and x.get("what") == "install")
                     or (x.get("kind") == "code" and "seconds" in x))
    return {"waiting_for_you": round(waiting, 1), "installing": round(installing, 1)}


def _machine_at_main(s: RunState, prof, checkout: Path) -> None:
    """The test machine now stands at main: recorded for later runs, and in this run's copy and state."""
    from . import fresh
    code = s["code"]
    fresh.record_machine(prof.repo, code["commit"])
    fresh.move(checkout, code["commit"], code["commit"])
    s["code"] = {**code, "built_from": code["commit"], "build": [], "build_cmd": "", "lock_changed": False,
                 "rebuild_pending": False, "rebuilt": True}


def _rebuild_at_main(s: RunState, prof, checkout: Path, plan: dict, seen) -> dict | None:
    """Hosted: the package list changed since the test machine was built; rebuild it at main, once."""
    import dataclasses
    import time
    from . import pkgcheck
    code = s["code"]
    events.log("code", key="rebuild", commit=code["commit"], reason="the package list changed since the test machine was built")
    t0 = time.monotonic()
    try:
        pkgcheck.rebuild(dataclasses.replace(prof, base_commit=code["commit"]), checkout)
    except Exception as ex:
        why = f"rebuilding the test machine at main failed: {str(ex)[:300]}"
        return {"repro": {"status": "NOT RUN", "ladder_plan": plan, "attempts_used": 0, "failing_test": None,
                          "oracle_test": None, "checkout": str(checkout), "sandbox_secrets_visible": seen},
                "outcome": stop(TEST_MACHINE_NOT_READY, f"{why}; no test was written"),
                "log": [f"reproduce: STOPPED {TEST_MACHINE_NOT_READY}: {why}"]}
    events.log("code", key="rebuilt", commit=code["commit"], seconds=round(time.monotonic() - t0))
    _machine_at_main(s, prof, checkout)
    return None


def _record_attempt(s: RunState, a: ladder.Attempt) -> ladder.Attempt:
    a.at = now()
    events.log("attempt", key=f"{a.rung}#{a.n}", **ladder.as_records([a])[0])  # written as it ends: survives a crash
    return a


def shelve_drafts(checkout: Path, rdir: Path, paths: list, keep: str | None) -> list[str]:
    """Only the judging test stays in the code; every other attempt's test moves to runs/<id>/attempt-tests/. Dev run
    2026-10-07: a correct fix turned the judge green, but leftover drafts (one crashing in its own setup, one
    unpassable) sat in the package and failed its suite."""
    moved = []
    for p in dict.fromkeys(p for p in paths if p and p != keep):
        src = Path(checkout) / p
        if src.exists():
            dest = Path(rdir) / "attempt-tests" / Path(p).name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if testwriter.tracked(checkout, p):  # the person's own test file: keep the cases, put the file back
                dest.write_text(src.read_text())
                src.write_text(testwriter.original(checkout, p))
            else:
                src.replace(dest)
            moved.append(p)
    return moved


def reproduce(s: RunState):
    s = dict(s)
    before = s.get("code")
    out = _reproduce(s)
    if s.get("code") is not before and isinstance(out, dict):   # the test machine was rebuilt at main in this step
        out["code"] = s["code"]
    return out


def _reproduce(s: RunState):
    work = run_dir(s) / "sandbox"
    work.mkdir(exist_ok=True)
    image = s["profile"]["image"]
    checkout = run_copy(profiles.get(s["profile"]["repo"]), run_dir(s) / "checkout", s.get("code"))  # made by Gather context
    # in the repo's own image (node for vercel/ai, python otherwise); hosted: in the repo's own E2B template
    seen = secrets_visible(checkout if CFG.sandbox_backend == "e2b" else work, image)
    if seen:
        raise GuardrailViolation(f"sandbox exposes secrets or failed its probe: {seen}")
    ctx = _ctx(s, checkout)
    # recorded data is a property of the code at fault, not of the repo (#22288: vercel/ai's providers have recordings,
    # its chat code has none, and three tries were spent looking for them)
    beside = bool(getattr(ctx, "fixtures", None))
    rungs, skipped = ladder.plan(s["triage"]["has_repro_p"], s["profile"].get("recorded_fixtures", False) and beside)
    if s["profile"].get("recorded_fixtures") and not beside:
        skipped["integration"] = f"no recorded data beside {Path(ctx.source).parent.as_posix()}"
    plan = {"rungs": [r.name for r in rungs], "skipped": skipped, "cap": REPRO_ATTEMPT_CAP}
    stopped = _install_if_missing(s, checkout, ctx, plan, seen)
    if stopped:
        return stopped
    existing = _existing_tests(s, checkout, ctx, profiles.get(s["profile"]["repo"]))
    if existing.get("status") == "CANNOT RUN":  # a test written here could never run: stop before any AI is spent
        why = f"the test machine cannot run the tests of {existing.get('package')}: {existing.get('why', '')}"
        return {"repro": {"status": "NOT RUN", "ladder_plan": plan, "existing_tests": existing, "attempts_used": 0,
                          "failing_test": None, "oracle_test": None, "located": ctx.source, "checkout": str(checkout),
                          "sandbox_secrets_visible": seen},
                "outcome": stop(TEST_MACHINE_NOT_READY, f"{why}; no test was written"),
                "log": [f"reproduce: STOPPED {TEST_MACHINE_NOT_READY}: {why}"]}
    before = [{k: v for k, v in e.items() if k in ladder.Attempt.__dataclass_fields__}
              for e in events.for_run(s["run_id"]) if e["kind"] == "attempt" and e["step"] == "reproduce"]
    result = ladder.climb(rungs, lambda r, n, h: _record_attempt(s, _write_and_run_test(s, r, n, h, ctx, checkout)),
                          already=before, skipped=skipped)
    new = [{"step": "reproduce", **a} for a in ladder.as_records(result.attempts[len(before):])]
    red = next((a for a in result.attempts if a.outcome == ladder.RED), None)
    # The judge for the fix: the recorded-stream test when it confirmed the bug (built from real data), else the first
    # RED. Dev run 2026-10-07: the made-up-input test was unpassable (its own chunk was invalid JSON); the recorded one
    # passed with the by-hand verified fix.
    confirming = next((a for a in result.attempts[result.attempts.index(red) + 1:]
                       if a.outcome == ladder.RED and a.rung == result.confirmed_by), None) if red else None
    oracle = confirming if (result.confirmed and confirming) else red
    into = getattr(ctx, "test_into", "")  # the person named the test file: its cases judge the fix when they show the bug
    oracle = next((a for a in result.attempts if into and a.outcome == ladder.RED and a.test_path == into), oracle)
    shelve_drafts(checkout, run_dir(s), [a.test_path for a in result.attempts], keep=oracle.test_path if oracle else None)
    out = {"repro": {"status": result.status, "rung": result.rung, "ladder_plan": plan,
                     "confirmed": result.confirmed, "confirmed_by": result.confirmed_by,
                     "failing_test": red.test_path if red else None, "evidence": red.evidence if red else None,
                     "oracle_test": oracle.test_path if oracle else None,
                     "oracle_evidence": oracle.evidence if oracle else None,
                     "attempts_used": len(result.attempts), "sandbox_secrets_visible": seen,
                     "located": ctx.source, "checkout": str(checkout), "existing_tests": existing,
                     "confirm_tries": (len(result.attempts) - result.attempts.index(red) - 1) if red else 0},
           "attempts": new,
           "log": [f"reproduce: {result.status}" + (f" at rung {result.rung}" if result.rung else "") +
                   f" after {len(result.attempts)} of {REPRO_ATTEMPT_CAP} attempts"
                   + {True: f"; confirmed on {result.confirmed_by}", False: "; NOT confirmed on a recorded stream",
                      None: ""}[result.confirmed if result.status == ladder.REPRODUCED else None]]}
    if result.status == ladder.NEVER_REPRODUCED:
        tried = ", ".join(f"{a.rung}:{a.outcome}" for a in result.attempts) or "no rung available"
        out["outcome"] = stop(ladder.NEVER_REPRODUCED, f"nothing went red ({tried}); no fix is written for a bug "
                                                       "we could not see")
    return out


def _locator_review(s: RunState, checkout: Path, ctx, cause: dict | None) -> dict:
    """cause-locator on the suspects: the cause found (if any) and the next files by the issue's strings, against the
    files of their packages and the failing test's output. Confidence is a stated convention, not a measurement: the
    cause found sits AT the bar (0.5: named, not yet proven by a fix); the others scale below it by match score."""
    prof = profiles.get(s["profile"]["repo"])
    lang = langs.of(prof)
    ranking = [(p, sc) for p, sc in (ctx.ranking or [])]
    top = max((sc for _, sc in ranking), default=1) or 1
    cands = []
    if cause:
        cands.append({"path": cause["file"], "lines": f"{cause['lines'][0]}-{cause['lines'][1]}",
                      "reason": (cause.get("why") or "")[:300], "confidence": 0.5})
    for path, sc in ranking:
        if len(cands) < 3 and path not in [c["path"] for c in cands]:
            cands.append({"path": path, "reason": "contains the issue's own strings", "confidence": round(0.5 * sc / top, 2)})
    pkgs = {lang.package_of(c["path"]) for c in cands}
    listed = subprocess.run(["git", "-C", str(checkout), "ls-files", "--", *lang.pathspec()], capture_output=True,
                            text=True, timeout=60).stdout.split()
    listing = [f for f in listed if lang.package_of(f) in pkgs][:2000]
    listing += [c["path"] for c in cands if c["path"] not in listing]
    r = s["repro"]
    return advisors.review(s, "find_cause", f"{s.get('focus') or s['issue']['title']}\n\n{(s['issue'].get('body') or '')[:1500]}",
                           question="Where is the cause?", context={
                               "repo_listing": listing, "repro_output": (r.get("oracle_evidence") or r.get("evidence") or "")[:6000],
                               "candidates": cands})


def find_cause(s: RunState):
    r = s["repro"]
    checkout = Path(r["checkout"])
    ctx = _ctx(s, checkout)
    try:
        cause = fixer.find_cause(s, checkout, ctx, r.get("oracle_test") or r["failing_test"],
                                 r.get("oracle_evidence") or r.get("evidence") or "", profiles.get(s["profile"]["repo"]))
    except fixer.FixRefused as e:
        return {"cause": {"status": "NOT FOUND", "why": str(e)}, "outcome": stop("CAUSE NOT FOUND", str(e)),
                "advisors": {"find_cause": _locator_review(s, checkout, ctx, None)},
                "log": [f"find_cause: STOPPED CAUSE NOT FOUND: {e}"]}
    a, b = cause["lines"]
    return {"cause": {"status": "FOUND", **cause}, "advisors": {"find_cause": _locator_review(s, checkout, ctx, cause)},
            "log": [f"find_cause: {cause['file']}:{a}-{b}"
                    + (f" (looked up {', '.join(cause['looked_up'])})" if cause["looked_up"] else "")]}


def write_fix(s: RunState):
    r, c = s["repro"], s["cause"]
    checkout = Path(r["checkout"])
    # tests stay as they are (the person's own test file may carry the cases that show the bug)
    dirty = [p for p in fixer._git(checkout, "diff", "--name-only").split() if p and not langs.is_test_path(p)]
    fixer.revert(checkout, dirty)  # a crash mid-step can leave a half-applied attempt; start from clean source
    prof = profiles.get(s["profile"]["repo"])
    looked = [fixer.find_definition(checkout, n, profile=prof) for n in c.get("looked_up", [])]
    judge = r.get("oracle_test") or r["failing_test"]
    judge_evidence = r.get("oracle_evidence") or r.get("evidence") or ""
    fix = fixer.write_fix(s, checkout, c, judge, judge_evidence, prof, looked_up=looked,
                          stale=dirty, drafts=run_dir(s) / "fixer")
    ho = None
    if fix["status"] == "VALIDATED":  # a second, independent judge written without seeing the fix
        base_copy = run_copy(prof, run_dir(s) / "holdout-base", s.get("code"))
        ctx = testwriter.locate(base_copy, s["issue"].get("body", ""), s.get("focus") or s["issue"]["title"], prof)
        ctx.extra = (s.get("context") or {}).get("brief", "")  # the second test reads the same gathered context
        ho = fixer.holdout(s, prof, checkout, base_copy, ctx, [judge], drafts=run_dir(s) / "holdout")
        if ho["status"] == "FIX INCOMPLETE":
            first = fix
            fixer.revert(checkout, first["changed"])
            fix = fixer.write_fix(
                s, checkout, c, judge, judge_evidence, prof, looked_up=looked, stale=first["changed"],
                drafts=run_dir(s) / "fixer-round2", judges=[judge, ho["test"]], attempts_max=2,
                prior=[f"a previous fix passed {Path(judge).name} and every suite, but FAILED a fresh test of the same "
                       f"problem ({Path(ho['test']).name}): {ho['evidence'][:600]}\nThat fix was:\n{first['patch'][:1500]}"])
            fix["attempts"] = first["attempts"] + fix["attempts"]
            if fix["status"] == "VALIDATED":
                # Ruled 2026-10-07 (north-star-v1.2): this fix saw the second test fail, so that test no longer judges
                # it blind. A fresh third test, written without seeing either fix, must also pass for two judges.
                third_copy = run_copy(prof, run_dir(s) / "holdout3-base", s.get("code"))
                ctx3 = testwriter.locate(third_copy, s["issue"].get("body", ""), s.get("focus") or s["issue"]["title"], prof)
                ctx3.extra = ctx.extra
                third = fixer.holdout(s, prof, checkout, third_copy, ctx3, [judge, ho["test"]],
                                      drafts=run_dir(s) / "holdout3")
                ho = {**ho, "third": third,
                      "status": fixer.THIRD_TEST_PASSED if third["status"] == "PASSED"
                      else f"SEEN (round 2: the fix saw this test; fresh third test {third['status']})"}
    fix["holdout"] = ho
    if fix["status"] == "VALIDATED":  # the proof, the other way round: every test that showed the bug now passes
        fix["after_fix"] = _after_fix(s, prof, checkout, [r.get("failing_test"), r.get("oracle_test")])
    clock = dict(s["fix_clock"])
    clock["stopped_at"] = now()
    total = (datetime.fromisoformat(clock["stopped_at"]) - datetime.fromisoformat(clock["started_at"])).total_seconds()
    clock["excluded"] = time_away(events.for_run(s["run_id"]), clock["started_at"], clock["stopped_at"])
    clock["seconds"] = round(max(0.0, total - sum(clock["excluded"].values())), 1)
    # ⏱ counts only a fix confirmed by TWO independent tests (its judge + a fresh one written without seeing it), with
    # every suite green. Dev run 2026-10-07: a one-judge "validated" fix failed all 3 by-hand reference tests.
    clock["judges"] = fixer.judge_count(fix["status"], ho)
    clock["validated"] = clock["judges"] == 2
    (run_dir(s) / "fix.patch").write_text(fix["patch"])
    out = {"fix": {k: v for k, v in fix.items() if k != "patch"} | {"patch_path": str(run_dir(s) / "fix.patch")},
           "fix_clock": clock,
           "log": [f"write_fix: {fix['status']} after {len(fix['attempts'])} attempt(s); fix clock {clock['seconds']}s"
                   + (f"; suites green: {', '.join(fix['suites'])}" if clock["validated"] else "")
                   + (f"; fresh test: {ho['status']}" if ho else "")]}
    if fix["status"] == "VALIDATED" and clock["judges"] == 1 and (ho or {}).get("status") != "FIX INCOMPLETE":
        why = ("the fix saw the second test and no fresh third test passed" if (ho or {}).get("third")
               else "the fresh test was inconclusive")
        out["log"].append(f"write_fix: ONE JUDGE ONLY: {why}, so this fix reaches the PR draft "
                          "labelled as such and does not count toward ⏱ time to validated fix")
    if fix["status"] == "TEST FLAWED":
        out["outcome"] = stop("TEST FLAWED", f"the fixer says the judging test cannot be passed: {fix['why'][:400]}. "
                                             "A person checks the test (the fixer may never edit it)")
    elif fix["status"] != "VALIDATED" or (ho or {}).get("status") == "FIX INCOMPLETE":
        last = (fix.get("why") or (fix["attempts"][-1]["evidence"] if fix["attempts"] else "no attempt"))[:300]
        out["outcome"] = stop("FIX NOT VALIDATED", f"{len(fix['attempts'])} attempt(s), none turned the test green with "
                                                   f"every affected suite passing. Last: {last}")
    return out


# ── Phase 2: Learn from it ──────────────────────────────────────────────────────────────────────
def _fix_patch(s: RunState) -> str:
    p = Path((s.get("fix") or {}).get("patch_path") or run_dir(s) / "fix.patch")
    return p.read_text() if p.exists() else ""


def why_it_shipped(s: RunState):
    """story.py: code gathers the evidence read-only, the model tells it, code checks it (no names, 2+ conditions,
    nothing cited that isn't in the evidence). Then the named condition is frozen BEFORE any guard exists."""
    try:
        told = story.tell(s, Path(s["repro"]["checkout"]), s["cause"], _fix_patch(s))
    except story.StoryRefused as e:
        return {"second_story": {"status": "NOT WRITTEN", "why": str(e)},
                "outcome": stop("STORY NOT WRITTEN", f"{e}. A person writes the second story"),
                "log": [f"why_it_shipped: STOPPED STORY NOT WRITTEN: {e}"]}
    assert_no_names(told["text"], [s["issue"]["reporter"]])  # guardrail 2, once more at the step boundary
    freeze = freeze_condition(LEDGER, s["run_id"], told["condition"])  # guardrail 3, before any guard exists
    ev = told["evidence"]
    prs = [e["pr"]["number"] for e in [ev.get("written") or {}] + ev["shaped"] if e.get("pr")]
    return {"second_story": {"status": "WRITTEN", "text": told["text"], "evidence": ev,
                             "names_checked": told["names_checked"]},
            # allspaw files the question as the fix's summary and the evidence as the incident: the report is scanned
            "advisors": {"why_it_shipped": advisors.review(
                s, "why_it_shipped", told["text"],
                question=f"The fix: {(s.get('cause') or {}).get('plan') or (s.get('cause') or {}).get('why') or 'see the report'}")},
            "condition": {"text": told["condition"], "sha256": freeze["sha256"], "frozen_at": freeze["frozen_at"]},
            "log": [f"why_it_shipped: story from {len(prs)} PR(s) ({', '.join('#' + str(n) for n in prs)}); "
                    f"no names ({told['names_checked']} checked); condition frozen {freeze['sha256'][:12]}"]}


A1_Q = {"a1": {"type": "choice",
               "instructions": "Would this guard still stop the bug if everyone forgot about it?",
               "criteria": {"CONDITION": "a test, lint rule, type or CI check that runs by itself",
                            "INSTRUCTION": "advice, documentation or a reminder a person must follow"}}}


def lasting_guard(s: RunState):
    """guard.py: one test over the CLASS of triggers; it must fail on the unfixed code, and its cases on the fixed code
    say which parts of the class this fix closed and which it left open. Plus every other site with the same line."""
    prof = profiles.get(s["profile"]["repo"])
    fixed = Path(s["repro"]["checkout"])
    unfixed = run_copy(prof, run_dir(s) / "holdout-base", s.get("code"))
    judge = s["repro"].get("oracle_test") or s["repro"]["failing_test"]
    ctx = testwriter.locate(unfixed, s["issue"].get("body", ""), s.get("focus") or s["issue"]["title"], prof)
    patch = _fix_patch(s)
    sig = story.signature_lines(patch)
    siblings = guard.sibling_sites(fixed, sig, s["cause"]["file"], prof)
    g = guard.write_guard(s, prof, fixed, unfixed, judge, s["cause"], patch, s["condition"]["text"],
                          (s.get("second_story") or {}).get("text", ""), ctx.example_header, run_dir(s) / "guard",
                          fixtures=ctx.fixtures)
    created = now()
    if g["status"] != "CATCHES THE BUG":
        return {"guard": {"status": g["status"], "why": g.get("why"), "siblings": siblings, "created_at": created,
                          "text": f"No guard: {g.get('why')}"},
                "outcome": stop("GUARD NOT WRITTEN", f"{g.get('why')}. A person writes the guard"),
                "log": [f"lasting_guard: STOPPED GUARD NOT WRITTEN: {g.get('why')}"]}
    a = decide(f"{g['covers']}. A test file that the package's test suite runs in CI on every change.", A1_Q)["a1"]
    open_cases = g["on_fixed"]["failed"]  # failing WITH the bug's symptom: a part of the class still open
    text = (f"{g['covers'].rstrip('. ')}. A parametrised test (`{Path(g['repo_path']).name}`) that fails on the old code, so it would "
            f"have caught this bug. On the fixed code {len(g['on_fixed']['passed'])} of "
            f"{len(g['on_fixed']['passed']) + len(open_cases)} cases pass.")
    # qe-ic-advisor files the question as the attempt and the evidence as the bug; it reads both for guard wording
    guard_said = (f"The guard is a test that runs by itself with the package's tests: the test fails whenever "
                  f"{g['covers'].rstrip('. ')}.")
    return {"advisors": {"lasting_guard": advisors.review(
                s, "lasting_guard", f"The bug: {s.get('focus') or s['issue']['title']}\n\n{text}", question=guard_said)},
            "guard": {"status": g["status"], "text": text, "covers": g["covers"], "path": g["path"],
                      "repo_path": g["repo_path"], "on_fixed": g["on_fixed"], "open_cases": open_cases,
                      "broken_cases": g.get("broken_cases", []) + g["on_fixed"].get("broken", []),
                      "siblings": siblings, "created_at": created, "a1_class": a["choice"],
                      "a1_p": a["answer_confidence"],
                      # what its symptom check used, and the cases that pass on the old code too (controls; review of
                      # run #22543: both were dropped, and the page said every case failed there)
                      "checked_against": g.get("checked_against") or [], "symptom_checked": g.get("symptom_checked"),
                      "unfixed_passed": (g.get("on_unfixed") or {}).get("passed") or []},
            "log": [f"lasting_guard: catches the bug on the unfixed code; fixed code passes "
                    f"{len(g['on_fixed']['passed'])}, still open {len(open_cases)}; {len(siblings)} sibling site(s); "
                    f"Laya A1 → {a['choice']} p={a['answer_confidence']}"]}


def _ensure_vector_index(dims: int, wait_s: int = 90) -> bool:
    """Create the index if missing, then wait until it can answer. False = the instrument can't look right now
    (e.g. it's rebuilding after a restart), which is UNEVALUABLE, never 'no siblings'."""
    import time
    if not list(CONDITIONS.list_search_indexes(VEC_INDEX)):
        from pymongo.operations import SearchIndexModel
        CONDITIONS.create_search_index(SearchIndexModel(name=VEC_INDEX, type="vectorSearch", definition={
            "fields": [{"type": "vector", "path": "embedding", "numDimensions": dims, "similarity": "cosine"}]}))
    for _ in range(wait_s):
        idx = list(CONDITIONS.list_search_indexes(VEC_INDEX))
        if idx and idx[0].get("queryable"):
            return True
        time.sleep(1)
    return False


SIMILAR_MIN = 0.8   # a stored bug this close (vector score) counts as the same kind; below, only "nearest"


def test_past_bugs(s: RunState):
    cond = s["condition"]["text"]
    verify_condition_frozen(LEDGER, s["run_id"], cond, s["guard"]["created_at"])  # guardrail 3
    import time
    t0 = time.monotonic()
    vec = embedder().embed_query(cond)  # search uses the condition only, never the guard
    events.log("embed", key="condition", model=CFG.embed_model, dims=len(vec), ms=round((time.monotonic() - t0) * 1000))
    hits, searched, can_look = [], CONDITIONS.estimated_document_count(), True
    others = len([u for u in CONDITIONS.distinct("issue_url") if u != s["issue_url"]])
    if others:
        can_look = _ensure_vector_index(len(vec))
        if can_look:
            hits = [h for h in CONDITIONS.aggregate([
                {"$vectorSearch": {"index": VEC_INDEX, "path": "embedding", "queryVector": vec,
                                   "numCandidates": 50, "limit": 5}},
                {"$project": {"_id": 0, "run_id": 1, "issue_url": 1, "text": 1,
                              "score": {"$meta": "vectorSearchScore"}}}])
                    if h["issue_url"] != s["issue_url"]]  # a sibling is a DIFFERENT issue, not an earlier run of this one
    close = [h for h in hits if h.get("score", 0) >= SIMILAR_MIN]
    stored = not cond.startswith("PLACEHOLDER")  # placeholders would show up as false siblings for every issue
    if stored:  # upsert: a resumed run that re-runs this step must not store its condition twice
        CONDITIONS.update_one({"run_id": s["run_id"]}, {"$set": {
            "issue_url": s["issue_url"], "text": cond, "embedding": vec, "sha256": s["condition"]["sha256"]}},
            upsert=True)
    if not others:
        state = "UNEVALUABLE (the corpus holds no other issue to search; not the same as no siblings)"
    elif not can_look:
        state = "UNEVALUABLE (search index not ready; not the same as no siblings)"
    elif close:
        state = "CANDIDATES FOUND"
    elif hits:   # review of run #22543: "1 similar bug" was the only other issue stored, about something else
        state = f"NO PAST SIBLING FOUND (nearest of {others} stored: {hits[0]['score']:.2f}, below {SIMILAR_MIN})"
    else:
        state = "NO PAST SIBLING FOUND"
    bt = _backtest_guard(s)
    result = {"state": state, "searched_conditions": searched, "other_issues": others, "candidates": close[:3],
              "would_have_caught": bt.get("would_have_caught") if bt else None,
              "false_alarms": bt.get("false_alarms") if bt else None, "detail": bt}
    fa = (bt or {}).get("false_alarms") or {}
    if bt and fa:
        note = (f"; back-test at the anchor: {bt['anchor']['state']}; before it: fired {fa['fired']}, quiet {fa['quiet']}, "
                f"bug already there {fa['bug_already_there']}, unevaluable {fa['unevaluable']} over "
                f"{fa['commits_covered']} of {fa['window']} commits")
    else:
        note = "; back-test not run: " + ((bt or {}).get("why") or "")
    return {"backtest": result, "log": [f"test_past_bugs: {state} ({len(hits)} candidates){note}"]}


def incident_tests(s: RunState) -> list[tuple[str, str]]:
    """Every test that reproduced THIS bug, judge first: the recorded-stream judge, the holdout written without seeing
    the fix, the first RED (shelved in attempt-tests/ when it wasn't the judge)."""
    r, fixed, out = s["repro"], Path(s["repro"]["checkout"]), []
    for rel in [r.get("oracle_test"), ((s.get("fix") or {}).get("holdout") or {}).get("test"), r.get("failing_test")]:
        if not rel or rel in [o[0] for o in out]:
            continue
        for src in (fixed / rel, run_dir(s) / "attempt-tests" / Path(rel).name):
            if src.exists():
                out.append((rel, src.read_text()))
                break
    return out


def _backtest_guard(s: RunState) -> dict | None:
    """🎯 would-have-caught + false alarms (backtest.py): the guard at the commit that wrote the bug, and before it."""
    g = s.get("guard") or {}
    w = ((s.get("second_story") or {}).get("evidence") or {}).get("written") or {}
    if g.get("status") != "CATCHES THE BUG":   # the page says the real reason (review of run #22543)
        return {"would_have_caught": None, "why": "there is no guard that catches the bug to test"}
    if not w.get("sha"):
        return {"would_have_caught": None, "why": "Why it slipped found no commit that wrote the line, so there is no "
                                                  "older version to start from"}
    prof = profiles.get(s["profile"]["repo"])
    return backtest.backtest(s["issue"], prof, base_path(prof), run_dir(s) / "history", langs.of(prof).package_of(g["repo_path"]),
                             judges=incident_tests(s),
                             guard_file=(g["repo_path"], Path(g["path"]).read_text()), anchor_sha=w["sha"],
                             focus=s.get("focus") or s["issue"]["title"],
                             evidence=s["repro"].get("oracle_evidence") or s["repro"].get("evidence") or "")


# ── Phase 3: Ship it ────────────────────────────────────────────────────────────────────────────
def reproduction_lines(r: dict) -> list[str]:
    """What the ladder found, said plainly, including when the recorded stream did NOT confirm it."""
    if r.get("status") != ladder.REPRODUCED:
        return []
    shown = next((l for l in (r.get("evidence") or "").splitlines() if "AssertionError" in l),
                 (r.get("evidence") or "").splitlines()[0] if r.get("evidence") else "")
    confirm = {True: f"confirmed on a recorded stream (rung `{r.get('confirmed_by')}`)",
               False: "**NOT confirmed**: the recorded-stream test passed, so the made-up input may be unrealistic",
               None: (f"recorded-stream check tried {r['confirm_tries']}× without a valid result: neither confirmed "
                      "nor refuted" if r.get("confirm_tries") else "not checked on a recorded stream (none available)")
               }[r.get("confirmed")]
    return ["## Reproduction",
            "Reproduced before any fix, in a sandbox with the network off, with staged input; **not observed live**.",
            f"- Failing test (rung `{r.get('rung')}`): `{r.get('failing_test')}`",
            *([f"- Judge for the fix (the recorded-stream test): `{r.get('oracle_test')}`"]
              if r.get("oracle_test") and r.get("oracle_test") != r.get("failing_test") else []),
            f"- It shows: `{shown.strip()[:300]}`",
            f"- {confirm}",
            f"- Attempts: {r.get('attempts_used')} of {REPRO_ATTEMPT_CAP}",
            ""]


def fix_lines(cause: dict, fix: dict, patch_path: Path) -> list[str]:
    if fix.get("status") != "VALIDATED":
        return ["## Fix", "PLACEHOLDER: the patch and the failing test it turns green.", ""]
    a, b = cause["lines"]
    patch = patch_path.read_text() if patch_path.exists() else ""
    return ["## Fix",
            f"Cause: `{cause['file']}` lines {a}-{b}. {cause['why']}",
            "",
            f"Validated before this text was written: the failing test now passes, and these suites stay green: "
            f"{', '.join(fix['suites'])}.",
            *([f"Not run: {len(fix['not_run'])} package(s) that depend on the changed code are not installed in the "
               f"sandbox ({', '.join(fix['not_run'][:8])}{', …' if len(fix['not_run']) > 8 else ''})."]
              if fix.get("not_run") else []),
            *([f"A second test of the same problem, written without seeing this fix, failed on the old code and "
               f"passes on the new: `{(fix.get('holdout') or {}).get('test')}`"
               + (f"; a fresh third test also passes (the fix had seen the second): "
                  f"`{((fix.get('holdout') or {}).get('third') or {}).get('test')}`"
                  if (fix.get("holdout") or {}).get("status") == fixer.THIRD_TEST_PASSED else "") + "."]
              if fixer.judge_count(fix.get("status", ""), fix.get("holdout")) == 2 else
              [f"**One judge only.** Second-test check: {(fix.get('holdout') or {}).get('status', 'not run')}; no "
               "independent test confirmed this fix, so it may cover only the path its one test exercises."]),
            "",
            "<details><summary>Patch</summary>", "", "```diff", patch.rstrip(), "```", "</details>", ""]


def backtest_lines(b: dict, g: dict | None = None) -> list[str]:
    d = (b or {}).get("detail") or {}
    if not d.get("anchor"):
        return [f"Past bugs of this kind: {b.get('state')}. Back-test: not run."]
    fa, a = d["false_alarms"], d["anchor"]
    caught = {True: "**yes**: the guard fails there", False: "**no**: the guard passes there",
              None: f"unevaluable ({a.get('why', 'the tests could not run on that code')})"}[d["would_have_caught"]]
    m = len(b.get("candidates") or [])
    return [f"Past bugs of this kind: {b.get('state')}.",
            f"🎯 Would-have-caught: " + ("**NOT SCORED** (m = 0: no earlier bug of the same kind to test)." if not m
                                        else f"**NOT SCORED YET**: {m} candidate(s), unconfirmed."),
            f"Self-check, not the 🎯 score (the guard was written from this bug): does it fail on the code where the "
            f"bug was written (`{a['sha']}`)? {caught}.",
            f"False alarms on the {fa['window']} commits before it: fired on **{fa['fired']}**; quiet on {fa['quiet']}; "
            f"the bug was already there on {fa['bug_already_there']} (the guard firing there is correct); "
            f"unevaluable on {fa['unevaluable']}; not run on {fa['not_run']} "
            f"({fa['groups_run']} groups run, commits grouped by whether they touched the guard's packages).",
            *([f"Where the bug is absent (this code with the fix), the guard fails on "
               f"{len((g or {}).get('open_cases', []))} of {sum(len(v) for v in ((g or {}).get('on_fixed') or {}).values())} cases."]
              if (g or {}).get("on_fixed") else []),
            *(["No commit in that window was free of the bug, so false alarms there cannot be counted; the fixed code "
               "is the only bug-free reference."] if fa["bug_already_there"] and not fa["quiet"] and not fa["fired"] else [])]


def commit_message(s: RunState) -> str:
    """The recommended commit message, the way commits are written upstream: `fix(scope): what the change does`, a
    short body (what was wrong, why this change), `Fixes #N`. Deterministic; Isha edits it before approving."""
    i, c = s["issue"], s.get("cause") or {}
    scope = Path(langs.of(profiles.get(s["profile"]["repo"])).package_of(c.get("file") or "x")).name
    scope = i["repo"] if scope in (".", "") else scope
    plan = re.sub(r"\s+", " ", (c.get("plan") or i["title"] or "").strip())
    first = c.get("subject") or re.split(r"(?<=[.;:])\s", plan)[0].rstrip(".;: ")
    # a plan sentence often opens with where ("In this provider's `flush`, decide …"): the title is the action
    first = re.sub(r"^(in|on|for|inside|within|when|at)\b[^,]{0,60},\s*", "", first, flags=re.I) if not c.get("subject") else first
    subject = f"fix({scope}): {first[:1].lower() + first[1:]}"
    if len(subject) > 72:
        subject = subject[:72].rsplit(" ", 1)[0]
    why = re.sub(r"\s+", " ", (c.get("why") or "").strip())
    body = "\n".join(textwrap.wrap(why, 72)) if why else ""
    return f"{subject}\n\n" + (f"{body}\n\n" if body else "") + f"Fixes #{i['number']}\n"


def _first_failure(evidence: str) -> str:
    for line in (evidence or "").splitlines():
        if "AssertionError" in line or "Error" in line:
            return line.strip()[:200]
    return ((evidence or "").splitlines() or [""])[0].strip()[:200]


def compose_pr_body(s: RunState, patch: str = "") -> str:
    """The pull request description in the shape GitHub PRs use (Isha 2026-10-08: "how it's actually done on git"):
    Summary, Changes, Tests (unit / integration / automation), Why it slipped, Follow-ups. The diff itself is the PR's
    files, not pasted here; its fingerprint closes the text, so approving the text approves exactly that change.
    Deterministic (no timestamps): the approval fingerprint must match on resume."""
    i, g, b = s["issue"], s["guard"], s["backtest"]
    c, f, r = s.get("cause") or {}, s.get("fix") or {}, s.get("repro") or {}
    files = diffview.parse(patch)
    src = [x for x in files if not diffview.is_test(x["path"])]
    tests = [x for x in files if diffview.is_test(x["path"])]
    src = [x for x in src if not x["path"].startswith(".changeset/")]
    ho = f.get("holdout") or {}
    held = {ho.get("test"), (ho.get("third") or {}).get("test")} - {None}
    judge = r.get("oracle_test") or r.get("failing_test")
    named = {pr_test_name(judge, s): judge} if judge else {}   # the PR's name for the judge → the run's
    two = fixer.judge_count(f.get("status", ""), ho) == 2
    out = ["## Summary",
           f"{_sentence(c.get('why'))} {_sentence(c.get('plan'))}".strip() or i["title"], "",
           f"Fixes #{i['number']}", ""]
    code = s.get("code") or {}
    if code.get("commit"):   # Isha 2026-10-10: say which main this was made and tested on
        out += [f"Made and tested on `main` at {code['commit'][:7]} (fetched {code.get('fetched_at', '')[:16].replace('T', ' ')} UTC).", ""]
    out += ["## Changes"]
    out += [f"- `{x['path']}` (+{x['added']} −{x['removed']})" for x in src] or ["- (no source change)"]
    out += ["", "## Tests"]
    skipped = (r.get("ladder_plan") or {}).get("skipped") or {}
    why_not = {"integration": "there is no recorded real data beside this code to build one from",
               "automation": "it would need live provider keys and the internet; the test machine has neither, on purpose"}
    for kind, label in (("unit", "Unit"), ("integration", "Integration"), ("automation", "Automation (end-to-end)")):
        mine = [x for x in tests if diffview.kind_of_test(x["path"]) == kind]
        for x in mine:
            x = {**x, "path": named.get(x["path"], x["path"])}
            if x["path"] in held:
                out.append(f"- **{label} test**, added `{x['path']}`: a second test of the same problem, written without "
                           "seeing this change; fails on `main`, passes with it.")
            else:
                ev = r.get("oracle_evidence") if x["path"] == r.get("oracle_test") else r.get("evidence")
                out.append(f"- **{label} test**, added `{pr_test_name(x['path'], s)}`: fails on `main` with "
                           f"`{_first_failure(ev)}`, passes with this change.")
        if not mine:
            reason = skipped.get("end_to_end" if kind == "automation" else kind)
            out.append(f"- **{label} test**: none added. " + (
                f"{why_not.get(kind, reason)[:1].upper()}{why_not.get(kind, reason)[1:]}." if reason else
                "The tests above show the problem and its fix."))
    kept = [t for t in sorted(held) if t != judge and t not in [x["path"] for x in tests]]
    if kept:
        out.append("- **Also checked, kept with the run** (not added here): " + "; ".join(f"`{Path(t).name}`" for t in kept)
                   + ", written without seeing this change; fails on `main`, passes with it.")
    if any(x["path"].startswith(".changeset/") for x in files):
        out.append("- **Changeset**: a patch release note for the changed package, as the repo asks.")
    ex = r.get("existing_tests") or {}
    if f.get("suites"):
        out.append(f"- **Existing tests**: still pass in {', '.join(f['suites'])}"
                   + (f" ({ex['passed']} tests in {ex.get('package')} before the change)." if ex.get("passed") else "."))
    if f.get("not_run"):
        out.append(f"- Not run: {len(f['not_run'])} dependent package(s) not installed in the test sandbox "
                   f"({', '.join(f['not_run'][:6])}{', …' if len(f['not_run']) > 6 else ''}).")
    if not two:
        out.append("- **One test only**: no second, independent test confirmed this change.")
    out += ["", "## Why this slipped through", s["condition"]["text"], "",
            "<details><summary>The full report (conditions, not people)</summary>", "", s["second_story"]["text"], "",
            "</details>", "", "## Follow-ups (not in this PR)", f"- A lasting guard for this class of bug: {g['text']}"]
    if g.get("open_cases"):
        out.append("- Still open after this change: " + "; ".join(f"`{x}`" for x in g["open_cases"]))
    if g.get("siblings"):
        out.append(f"- The same code is in {len(g['siblings'])} other file(s), not changed here: "
                   + ", ".join(f"`{x}`" for x in g["siblings"][:10]))
    out += [f"- {line}" for line in backtest_lines(b, g)[:1]]
    out += ["", f"<!-- debugassist: change sha256 {fingerprint(patch)} -->"]
    return "\n".join(out)


def _sentence(text) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    return t if not t or t.endswith((".", "!", "?")) else t + "."


def pr_test_name(rel: str, s: RunState) -> str:
    """The name a run's test takes in the pull request: after the file it tests and the issue, not the pipeline
    (review of run #22543: `da-repro-22543-unit-2.ui.test.ts` → `use-object.issue-22543.ui.test.ts`)."""
    stem = Path((s.get("cause") or {}).get("file") or "x").stem.split(".")[0]
    n = s["issue"]["number"]
    if Path(rel).name.startswith("test_da_"):   # pytest
        return str(Path(rel).with_name(f"test_{stem}_issue_{n}.py"))
    return re.sub(rf"da-repro-{n}(?:-[a-z]+)?(?:-\d+)?(?=\.)", f"{stem}.issue-{n}", rel)


def _split_patch(patch: str) -> list[tuple[str, str]]:
    parts = re.split(r"(?m)^(?=diff --git )", patch or "")
    return [(m.group(1), p) for p in parts if (m := re.match(r"diff --git a/(.+?) b/", p))]


def _changeset(s: RunState, checkout: Path, src_paths: list[str]) -> str:
    """A repo that keeps a `.changeset/` folder wants one with every package change (vercel/ai's CONTRIBUTING; review
    of run #22543: the PR had none). A patch release of each package the fix changed."""
    if not (checkout / ".changeset").is_dir() or not src_paths:
        return ""
    lang, names = langs.of(profiles.get(s["profile"]["repo"])), []
    for path in src_paths:
        try:
            name = json.loads((checkout / lang.package_of(path) / "package.json").read_text()).get("name")
        except (OSError, ValueError):
            name = None
        if name and name not in names:
            names.append(name)
    if not names:
        return ""
    text = "---\n" + "".join(f"'{n}': patch\n" for n in names) + "---\n\n" + commit_message(s).splitlines()[0] + "\n"
    return diffview.new_file_diff(f".changeset/debugassist-fix-{s['issue']['number']}.md", text)


def _pr_change(s: RunState) -> str:
    """The change the PR carries: the fix, ONE test (the one that judged it), named after the file it tests, and a
    changeset where the repo keeps them. Review of run #22543: two near-identical run tests in src/ and no changeset
    made the PR unmergeable as written; the other tests are named in the PR text and stay with the run."""
    checkout = Path((s.get("repro") or {}).get("checkout") or run_dir(s) / "checkout")
    patch = diffview.pr_patch(checkout) if (checkout / ".git").exists() else ""
    r = s.get("repro") or {}
    judge = r.get("oracle_test") or r.get("failing_test")
    out, src = "", []
    for path, part in _split_patch(patch):
        if diffview.is_test(path) and "new file mode" in part.split("@@", 1)[0] and path != judge:
            continue   # another run test: named in the PR text, kept with the run
        if diffview.is_test(path) and path == judge and "new file mode" in part.split("@@", 1)[0]:
            part = part.replace(path, pr_test_name(path, s))
        elif not diffview.is_test(path):
            src.append(path)
        out += part if part.endswith("\n") else part + "\n"
    return out + _changeset(s, checkout, src)


def approval(s: RunState):
    patch = _pr_change(s)
    (run_dir(s) / "pr.patch").write_text(patch)
    msg_path = run_dir(s) / "commit-message.txt"
    if not msg_path.exists():   # the recommendation; your edit (from Check PR) replaces it before approving
        msg_path.write_text(commit_message(s))
    body = compose_pr_body(s, patch)
    path = run_dir(s) / "PR.md"
    path.write_text(body)
    answer = interrupt({"pr_body_path": str(path), "sha256": fingerprint(body), "patch_path": str(run_dir(s) / "pr.patch"),
                        "commit_message_path": str(msg_path),
                        "ask": "Reply 'go' to approve this exact text; anything else rejects it"})
    if str(answer).strip().lower() != "go":
        events.log("approval", key="approval", status="REJECTED", sha256=fingerprint(body))
        return {"approval": {"status": "REJECTED", "answer": str(answer)}, "outcome": stop("REJECTED", "you said no"),
                "log": ["approval: REJECTED"]}
    rec = record_approval(LEDGER, s["run_id"], body, approver="isha")  # guardrail 1
    events.log("approval", key="approval", status="APPROVED", sha256=rec["sha256"])
    return {"pr_body": body, "approval": {"status": "APPROVED", "sha256": rec["sha256"], "at": rec["at"]},
            "log": [f"approval: APPROVED {rec['sha256'][:12]}"]}


def open_pr(s: RunState):
    verify_approval(LEDGER, s["run_id"], s["pr_body"])  # guardrail 1: refuses changed text
    i = s["issue"]
    script = run_dir(s) / "publish.sh"
    msg = run_dir(s) / "commit-message.txt"
    title = (msg.read_text().splitlines() or [f"fix: #{i['number']}"])[0] if msg.exists() else f"fix: #{i['number']}"
    branch = f"debugassist/fix-{i['number']}"
    code = s.get("code") or {}
    start = ([f"echo git fetch origin {code['commit']}",   # the branch starts at the main this run was made and tested on
              f"echo git switch -c {branch} {code['commit']}"] if code.get("commit") else [f"echo git switch -c {branch}"])
    script.write_text("\n".join([
        "#!/bin/sh",
        "# You run this, with your own GitHub login. The pipeline holds a read-only token and cannot publish.",
        "# It prints the steps; run them yourself in a clone of the repo (the change is pr.patch, the message is yours).",
        *([f"# Branches from main at {code['commit'][:7]}, fetched {code.get('fetched_at', '')} (the run's code)"] if code.get("commit") else []),
        *start,
        f"echo git apply \"{run_dir(s) / 'pr.patch'}\"",
        f"echo git commit -a -F \"{msg}\"",
        f"echo gh pr create --repo {i['owner']}/{i['repo']} --draft --head {branch} --title \"{title}\" "
        f"--body-file \"{run_dir(s) / 'PR.md'}\"",
    ]))
    script.chmod(0o755)
    return {"published": {"status": "DRY RUN: you publish", "script": str(script)},
            "outcome": stop("READY FOR YOU TO PUBLISH", f"approved text verified; run {script}"),
            "log": [f"open_pr: verified approval; wrote {script.name} for you to run"]}


def _after_approval(s: RunState):
    return "open_pr" if s.get("approval", {}).get("status") == "APPROVED" else END


def _unless_stopped(next_step: str):
    """A typed exit ends the run here; otherwise go on."""
    def route(s: RunState):
        return END if s.get("outcome") else next_step
    route.__name__ = f"to_{next_step}_unless_stopped"
    return route


def build():
    g = StateGraph(RunState)
    steps = [read_issue, gather_context, reproduce, find_cause, write_fix, why_it_shipped, lasting_guard,
             test_past_bugs, approval]
    for fn in steps + [open_pr]:
        g.add_node(fn.__name__, step(fn))
    g.add_edge(START, steps[0].__name__)
    can_stop = {"read_issue", "gather_context", "reproduce", "find_cause", "write_fix", "why_it_shipped", "lasting_guard"}
    for a, b in zip(steps, steps[1:]):
        if a.__name__ in can_stop:
            g.add_conditional_edges(a.__name__, _unless_stopped(b.__name__), [b.__name__, END])
        else:
            g.add_edge(a.__name__, b.__name__)
    g.add_conditional_edges("approval", _after_approval, ["open_pr", END])
    g.add_edge("open_pr", END)
    return g.compile(checkpointer=MongoDBSaver(client(), db_name=CFG.db_name))
