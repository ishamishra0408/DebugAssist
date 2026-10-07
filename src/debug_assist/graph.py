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
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.checkpoint.mongodb import MongoDBSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from . import backtest, events, fixer, guard, ladder, story, testwriter
from .checkout import base_path, run_copy
from .config import CFG, REPRO_ATTEMPT_CAP
from .github_read import get_issue
from .issue_text import TRIAGE_QUESTIONS, clean
from .guardrails import (GuardrailViolation, assert_no_names, fingerprint, freeze_condition, now,
                         record_approval, verify_approval, verify_condition_frozen)
from .models import decide, embedder, write
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
    attempts: Annotated[list, operator.add]  # append-only: every attempt and how it ended (also in the event log)
    outcome: dict                            # set when the run stops: {"exit": ..., "why": ..., "at": ...}
    log: Annotated[list, operator.add]


def step(fn):
    """Bind (run_id, step) while a step runs, so every model call, sandbox command and Laya decision inside it is
    metered and logged under this run."""
    @functools.wraps(fn)
    def bound(s: RunState):
        with events.bind(s["run_id"], fn.__name__):
            return fn(s)
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
    issue = get_issue(s["issue_url"])
    text = clean(issue["title"], issue["body"])  # the exact input format the triage model was trained on
    t = decide(text, TRIAGE_QUESTIONS, which="triage")
    g = decide(text, REPRO_Q, which="general")
    p = t["is_defect"]["noul"]
    triage = {"is_defect_p": p, "kind": t["kind"]["choice"], "kind_p": t["kind"]["answer_confidence"],
              "has_repro_p": g["has_repro"]["noul"], "regression_p": g["regression"]["noul"],
              "needs_person": max(p, 1 - p) < CFG.triage_review_below, "model": "laya-triage (fine-tuned)"}
    prof = profile_for(issue["owner"], issue["repo"])
    out = {"issue": issue, "triage": triage, "fix_clock": {"started_at": now()}, "spent_usd": 0.0, "turns": {},
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
    return out


def focus_of(issue: dict, focus: str | None, heading: str | None) -> str:
    """The problem to reproduce: given outright, or the text under a markdown heading of the issue (e.g. #21439's
    "Secondary observation"). Empty when the heading isn't there."""
    if focus:
        return focus.strip()
    if not heading:
        return ""
    import re
    m = re.search(rf"^#+\s*{re.escape(heading)}\s*$\n(.*?)(?=^#+\s|\Z)", issue.get("body", ""), re.M | re.S | re.I)
    return m.group(1).strip() if m else ""


def _write_and_run_test(s: RunState, rung: ladder.Rung, n: int, history: list, ctx, checkout) -> ladder.Attempt:
    """One rung attempt (testwriter.py): the writer drafts a test for this rung, seeing every earlier attempt and its
    evidence; the sandbox runs it with the network off; classify() and the right-reason check decide."""
    drafts = run_dir(s) / "writer"
    drafts.mkdir(exist_ok=True)
    return testwriter.attempt(s, rung, n, history, ctx, checkout, PROFILES[s["profile"]["repo"]], drafts=drafts)


def _record_attempt(s: RunState, a: ladder.Attempt) -> ladder.Attempt:
    a.at = now()
    events.log("attempt", **ladder.as_records([a])[0])  # written the moment it ends: survives a crash mid-step
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
            src.replace(dest)
            moved.append(p)
    return moved


def reproduce(s: RunState):
    work = run_dir(s) / "sandbox"
    work.mkdir(exist_ok=True)
    image = s["profile"]["image"]
    seen = secrets_visible(work, image)  # in the repo's own image (node for vercel/ai, python otherwise)
    if seen:
        raise GuardrailViolation(f"sandbox exposes secrets or failed its probe: {seen}")
    rungs, skipped = ladder.plan(s["triage"]["has_repro_p"], s["profile"].get("recorded_fixtures", False))
    plan = {"rungs": [r.name for r in rungs], "skipped": skipped, "cap": REPRO_ATTEMPT_CAP}
    checkout = run_copy(PROFILES[s["profile"]["repo"]], run_dir(s) / "checkout")  # this run's own unmodified code
    ctx = testwriter.locate(checkout, s["issue"].get("body", ""), s.get("focus") or s["issue"]["title"])
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
    shelve_drafts(checkout, run_dir(s), [a.test_path for a in result.attempts], keep=oracle.test_path if oracle else None)
    out = {"repro": {"status": result.status, "rung": result.rung, "ladder_plan": plan,
                     "confirmed": result.confirmed, "confirmed_by": result.confirmed_by,
                     "failing_test": red.test_path if red else None, "evidence": red.evidence if red else None,
                     "oracle_test": oracle.test_path if oracle else None,
                     "oracle_evidence": oracle.evidence if oracle else None,
                     "attempts_used": len(result.attempts), "sandbox_secrets_visible": seen,
                     "located": ctx.source, "checkout": str(checkout),
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


def find_cause(s: RunState):
    r = s["repro"]
    checkout = Path(r["checkout"])
    ctx = testwriter.locate(checkout, s["issue"].get("body", ""), s.get("focus") or s["issue"]["title"])
    try:
        cause = fixer.find_cause(s, checkout, ctx, r.get("oracle_test") or r["failing_test"],
                                 r.get("oracle_evidence") or r.get("evidence") or "")
    except fixer.FixRefused as e:
        return {"cause": {"status": "NOT FOUND", "why": str(e)}, "outcome": stop("CAUSE NOT FOUND", str(e)),
                "log": [f"find_cause: STOPPED CAUSE NOT FOUND: {e}"]}
    a, b = cause["lines"]
    return {"cause": {"status": "FOUND", **cause},
            "log": [f"find_cause: {cause['file']}:{a}-{b}"
                    + (f" (looked up {', '.join(cause['looked_up'])})" if cause["looked_up"] else "")]}


def write_fix(s: RunState):
    r, c = s["repro"], s["cause"]
    checkout = Path(r["checkout"])
    dirty = [p for p in fixer._git(checkout, "diff", "--name-only").split() if p]
    fixer.revert(checkout, dirty)  # a crash mid-step can leave a half-applied attempt; start from clean source
    prof = PROFILES[s["profile"]["repo"]]
    looked = [fixer.find_definition(checkout, n) for n in c.get("looked_up", [])]
    judge = r.get("oracle_test") or r["failing_test"]
    judge_evidence = r.get("oracle_evidence") or r.get("evidence") or ""
    fix = fixer.write_fix(s, checkout, c, judge, judge_evidence, prof, looked_up=looked,
                          stale=dirty, drafts=run_dir(s) / "fixer")
    ho = None
    if fix["status"] == "VALIDATED":  # a second, independent judge written without seeing the fix
        base_copy = run_copy(prof, run_dir(s) / "holdout-base")
        ctx = testwriter.locate(base_copy, s["issue"].get("body", ""), s.get("focus") or s["issue"]["title"])
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
                ho = {**ho, "status": "PASSED (round 2)"}
    fix["holdout"] = ho
    clock = dict(s["fix_clock"])
    clock["stopped_at"] = now()
    clock["seconds"] = round((datetime.fromisoformat(clock["stopped_at"])
                              - datetime.fromisoformat(clock["started_at"])).total_seconds(), 1)
    # ⏱ counts only a fix confirmed by TWO independent tests (its judge + a fresh one written without seeing it), with
    # every suite green. Dev run 2026-10-07: a one-judge "validated" fix failed all 3 by-hand reference tests.
    two_judges = str((ho or {}).get("status", "")).startswith("PASSED")
    clock["validated"] = fix["status"] == "VALIDATED" and two_judges
    clock["judges"] = 2 if two_judges else (1 if fix["status"] == "VALIDATED" else 0)
    (run_dir(s) / "fix.patch").write_text(fix["patch"])
    out = {"fix": {k: v for k, v in fix.items() if k != "patch"} | {"patch_path": str(run_dir(s) / "fix.patch")},
           "fix_clock": clock,
           "log": [f"write_fix: {fix['status']} after {len(fix['attempts'])} attempt(s); fix clock {clock['seconds']}s"
                   + (f"; suites green: {', '.join(fix['suites'])}" if clock["validated"] else "")
                   + (f"; fresh test: {ho['status']}" if ho else "")]}
    if fix["status"] == "VALIDATED" and not two_judges and (ho or {}).get("status") != "FIX INCOMPLETE":
        out["log"].append("write_fix: ONE JUDGE ONLY: the fresh test was inconclusive, so this fix reaches the PR draft "
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
    prof = PROFILES[s["profile"]["repo"]]
    fixed = Path(s["repro"]["checkout"])
    unfixed = run_copy(prof, run_dir(s) / "holdout-base")
    judge = s["repro"].get("oracle_test") or s["repro"]["failing_test"]
    ctx = testwriter.locate(unfixed, s["issue"].get("body", ""), s.get("focus") or s["issue"]["title"])
    patch = _fix_patch(s)
    sig = story.signature_lines(patch)
    siblings = guard.sibling_sites(fixed, sig, s["cause"]["file"])
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
    return {"guard": {"status": g["status"], "text": text, "covers": g["covers"], "path": g["path"],
                      "repo_path": g["repo_path"], "on_fixed": g["on_fixed"], "open_cases": open_cases,
                      "broken_cases": g.get("broken_cases", []) + g["on_fixed"].get("broken", []),
                      "siblings": siblings, "created_at": created, "a1_class": a["choice"],
                      "a1_p": a["answer_confidence"]},
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


def test_past_bugs(s: RunState):
    cond = s["condition"]["text"]
    verify_condition_frozen(LEDGER, s["run_id"], cond, s["guard"]["created_at"])  # guardrail 3
    vec = embedder().embed_query(cond)  # search uses the condition only, never the guard
    hits, searched, can_look = [], CONDITIONS.estimated_document_count(), True
    if searched:
        can_look = _ensure_vector_index(len(vec))
        if can_look:
            hits = [h for h in CONDITIONS.aggregate([
                {"$vectorSearch": {"index": VEC_INDEX, "path": "embedding", "queryVector": vec,
                                   "numCandidates": 50, "limit": 5}},
                {"$project": {"_id": 0, "run_id": 1, "issue_url": 1, "text": 1,
                              "score": {"$meta": "vectorSearchScore"}}}])
                    if h["issue_url"] != s["issue_url"]]  # a sibling is a DIFFERENT issue, not an earlier run of this one
    stored = not cond.startswith("PLACEHOLDER")  # placeholders would show up as false siblings for every issue
    if stored:  # upsert: a resumed run that re-runs this step must not store its condition twice
        CONDITIONS.update_one({"run_id": s["run_id"]}, {"$set": {
            "issue_url": s["issue_url"], "text": cond, "embedding": vec, "sha256": s["condition"]["sha256"]}},
            upsert=True)
    if not can_look:
        state = "UNEVALUABLE (search index not ready; not the same as no siblings)"
    elif hits:
        state = "CANDIDATES FOUND (back-test runs Friday)"
    else:
        state = "NO PAST SIBLING FOUND"
    bt = _backtest_guard(s)
    result = {"state": state, "searched_conditions": searched, "candidates": hits[:3],
              "would_have_caught": bt.get("would_have_caught") if bt else None,
              "false_alarms": bt.get("false_alarms") if bt else None, "detail": bt}
    fa = (bt or {}).get("false_alarms") or {}
    if bt and fa:
        note = (f"; back-test at the anchor: {bt['anchor']['state']}; before it: fired {fa['fired']}, quiet {fa['quiet']}, "
                f"bug already there {fa['bug_already_there']}, unevaluable {fa['unevaluable']} over "
                f"{fa['commits_covered']} of {fa['window']} commits")
    else:
        note = "; back-test not run: " + ((bt or {}).get("why") or "no guard or no anchor commit")
    return {"backtest": result, "log": [f"test_past_bugs: {state} ({len(hits)} candidates){note}"]}


def _backtest_guard(s: RunState) -> dict | None:
    """🎯 would-have-caught + false alarms (backtest.py): the guard at the commit that wrote the bug, and before it."""
    g = s.get("guard") or {}
    w = ((s.get("second_story") or {}).get("evidence") or {}).get("written") or {}
    if g.get("status") != "CATCHES THE BUG" or not w.get("sha"):
        return None
    prof = PROFILES[s["profile"]["repo"]]
    judge = s["repro"].get("oracle_test") or s["repro"]["failing_test"]
    fixed = Path(s["repro"]["checkout"])
    return backtest.backtest(s["issue"], prof, base_path(prof), run_dir(s) / "history", g["repo_path"].split("/")[1],
                             judge=(judge, (fixed / judge).read_text()),
                             guard_file=(g["repo_path"], Path(g["path"]).read_text()), anchor_sha=w["sha"],
                             focus=s.get("focus") or s["issue"]["title"])


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
            *([f"A second test of the same problem, written without seeing this fix, failed on the old code and "
               f"passes on the new: `{(fix.get('holdout') or {}).get('test')}`."]
              if str((fix.get("holdout") or {}).get("status", "")).startswith("PASSED") else
              [f"**One judge only.** Second-test check: {(fix.get('holdout') or {}).get('status', 'not run')}; no "
               "independent test confirmed this fix, so it may cover only the path its one test exercises."]),
            "",
            "<details><summary>Patch</summary>", "", "```diff", patch.rstrip(), "```", "</details>", ""]


def backtest_lines(b: dict) -> list[str]:
    d = (b or {}).get("detail") or {}
    if not d.get("anchor"):
        return [f"Past bugs of this kind: {b.get('state')}. Back-test: not run."]
    fa, a = d["false_alarms"], d["anchor"]
    caught = {True: "**yes**: the guard fails there", False: "**no**: the guard passes there",
              None: f"unevaluable ({a.get('why', 'the tests could not run on that code')})"}[d["would_have_caught"]]
    return [f"Past bugs of this kind: {b.get('state')}.",
            f"Back-test, would it have caught this bug when it was written (`{a['sha']}`)? {caught}.",
            f"False alarms on the {fa['window']} commits before it: fired on **{fa['fired']}**; quiet on {fa['quiet']}; "
            f"the bug was already there on {fa['bug_already_there']} (the guard firing there is correct); "
            f"unevaluable on {fa['unevaluable']}; not run on {fa['not_run']} "
            f"({fa['groups_run']} groups run, commits grouped by whether they touched the guard's packages)."]


def compose_pr_body(s: RunState) -> str:
    """Deterministic (no timestamps): the approval fingerprint must match on resume."""
    i, g, b = s["issue"], s["guard"], s["backtest"]
    return "\n".join([
        f"Fixes #{i['number']}",
        "",
        *reproduction_lines(s.get("repro") or {}),
        *fix_lines(s.get("cause") or {}, s.get("fix") or {}, run_dir(s) / "fix.patch"),
        "<details><summary>Why this shipped (conditions, not people)</summary>",
        "",
        s["second_story"]["text"],
        "",
        f"Named condition: {s['condition']['text']}",
        "</details>",
        "",
        "## Lasting guard",
        g["text"],
        *([f"Still open after this fix (cases of the class it does not close): " +
           "; ".join(f"`{c}`" for c in g["open_cases"])] if g.get("open_cases") else []),
        *([f"The same code exists in {len(g['siblings'])} other file(s), not changed here: " +
           ", ".join(f"`{x}`" for x in g["siblings"][:10])] if g.get("siblings") else []),
        *backtest_lines(b),
        "",
        "Does this match how it looked when the original change was written? Corrections welcome.",
        "",
        "_Draft generated by Debug Assist. Every claim above was checked by code before this text was written._",
    ])


def approval(s: RunState):
    body = compose_pr_body(s)
    path = run_dir(s) / "PR.md"
    path.write_text(body)
    answer = interrupt({"pr_body_path": str(path), "sha256": fingerprint(body),
                        "ask": "Reply 'go' to approve this exact text; anything else rejects it"})
    if str(answer).strip().lower() != "go":
        return {"approval": {"status": "REJECTED", "answer": str(answer)}, "outcome": stop("REJECTED", "you said no"),
                "log": ["approval: REJECTED"]}
    rec = record_approval(LEDGER, s["run_id"], body, approver="isha")  # guardrail 1
    return {"pr_body": body, "approval": {"status": "APPROVED", "sha256": rec["sha256"], "at": rec["at"]},
            "log": [f"approval: APPROVED {rec['sha256'][:12]}"]}


def open_pr(s: RunState):
    verify_approval(LEDGER, s["run_id"], s["pr_body"])  # guardrail 1: refuses changed text
    i = s["issue"]
    script = run_dir(s) / "publish.sh"
    script.write_text("\n".join([
        "#!/bin/sh",
        "# You run this, with your own GitHub login. The pipeline holds a read-only token and cannot publish.",
        "# SKELETON: there is no fix branch yet, so this only prints the command.",
        f"echo gh pr create --repo {i['owner']}/{i['repo']} --draft --title \"fix: #{i['number']}\" "
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
    steps = [read_issue, reproduce, find_cause, write_fix, why_it_shipped, lasting_guard, test_past_bugs, approval]
    for fn in steps + [open_pr]:
        g.add_node(fn.__name__, step(fn))
    g.add_edge(START, steps[0].__name__)
    can_stop = {"read_issue", "reproduce", "find_cause", "write_fix", "why_it_shipped", "lasting_guard"}
    for a, b in zip(steps, steps[1:]):
        if a.__name__ in can_stop:
            g.add_conditional_edges(a.__name__, _unless_stopped(b.__name__), [b.__name__, END])
        else:
            g.add_edge(a.__name__, b.__name__)
    g.add_conditional_edges("approval", _after_approval, ["open_pr", END])
    g.add_edge("open_pr", END)
    return g.compile(checkpointer=MongoDBSaver(client(), db_name=CFG.db_name))
