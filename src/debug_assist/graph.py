"""The pipeline: 9 steps in a fixed order (Uber's fixed-plan idea), in three phases.

  Fix it       read_issue → reproduce → find_cause → write_fix          (⏱ fix clock: pickup → validated fix)
  Learn        why_it_shipped → lasting_guard → test_past_bugs          (🎯 would-have-caught)
  Ship         approval (pauses for your go-word) → open_pr (dry run: you publish)

WALKING SKELETON (2026-10-06): steps marked PLACEHOLDER return stand-in content. Everything else is real:
GitHub read, Laya triage, sandbox secret probe, the reproduction ladder's plan, one tiny model call, the spend
meter, condition freeze, vector search, the approval fingerprint and the read-only publish path.

Typed early exits (Thursday): a run can stop on purpose, with a named reason in `outcome`, instead of pushing on:
  NEEDS PERSON       triage confidence below the review bar (a person reads the issue first)
  NOT A DEFECT       triage is confident it is not a bug
  NEVER REPRODUCED   the ladder used its attempts and nothing went red (no fix for a bug we could not see)
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

from . import events, fixer, ladder, testwriter
from .checkout import run_copy
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
    fix = fixer.write_fix(s, checkout, c, r.get("oracle_test") or r["failing_test"],
                          r.get("oracle_evidence") or r.get("evidence") or "", prof, looked_up=looked,
                          stale=dirty, drafts=run_dir(s) / "fixer")
    clock = dict(s["fix_clock"])
    clock["stopped_at"] = now()
    clock["seconds"] = round((datetime.fromisoformat(clock["stopped_at"])
                              - datetime.fromisoformat(clock["started_at"])).total_seconds(), 1)
    clock["validated"] = fix["status"] == "VALIDATED"  # ⏱ counts only a fix that turned the test green, suites green
    (run_dir(s) / "fix.patch").write_text(fix["patch"])
    out = {"fix": {k: v for k, v in fix.items() if k != "patch"} | {"patch_path": str(run_dir(s) / "fix.patch")},
           "fix_clock": clock,
           "log": [f"write_fix: {fix['status']} after {len(fix['attempts'])} attempt(s); fix clock {clock['seconds']}s"
                   + (f"; suites green: {', '.join(fix['suites'])}" if clock["validated"] else "")]}
    if fix["status"] == "TEST FLAWED":
        out["outcome"] = stop("TEST FLAWED", f"the fixer says the judging test cannot be passed: {fix['why'][:400]}. "
                                             "A person checks the test (the fixer may never edit it)")
    elif not clock["validated"]:
        last = fix["attempts"][-1]["evidence"][:300] if fix["attempts"] else "no attempt"
        out["outcome"] = stop("FIX NOT VALIDATED", f"{len(fix['attempts'])} attempt(s), none turned the test green with "
                                                   f"every affected suite passing. Last: {last}")
    return out


# ── Phase 2: Learn from it ──────────────────────────────────────────────────────────────────────
def why_it_shipped(s: RunState):
    story = ("PLACEHOLDER second story. Friday: critical junctures (written → reviewed → released → reported), "
             "what was known at each, two or more conditions that only together let it ship, and where the "
             "analysis stopped and why.")
    condition = f"PLACEHOLDER condition for: {s['issue']['title']}"
    assert_no_names(story, [s["issue"]["reporter"]])  # guardrail 2
    freeze = freeze_condition(LEDGER, s["run_id"], condition)  # guardrail 3, before any guard exists
    return {"second_story": {"status": "PLACEHOLDER", "text": story},
            "condition": {"text": condition, "sha256": freeze["sha256"], "frozen_at": freeze["frozen_at"]},
            "log": [f"why_it_shipped: PLACEHOLDER; no names; condition frozen {freeze['sha256'][:12]}"]}


A1_Q = {"a1": {"type": "choice",
               "instructions": "Would this guard still stop the bug if everyone forgot about it?",
               "criteria": {"CONDITION": "a test, lint rule, type or CI check that runs by itself",
                            "INSTRUCTION": "advice, documentation or a reminder a person must follow"}}}


def lasting_guard(s: RunState):
    text = "PLACEHOLDER guard: a parametrised test in CI that covers the whole class of input, not one case."
    a = decide(text, A1_Q)["a1"]
    guard = {"status": "PLACEHOLDER", "text": text, "created_at": now(),
             "a1_class": a["choice"], "a1_p": a["answer_confidence"]}
    return {"guard": guard, "log": [f"lasting_guard: PLACEHOLDER; Laya A1 → {a['choice']} p={a['answer_confidence']}"]}


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
    backtest = {"state": state, "searched_conditions": searched, "candidates": hits[:3],
                "would_have_caught": None, "false_alarms": None}
    return {"backtest": backtest, "log": [f"test_past_bugs: {backtest['state']} ({len(hits)} candidates)"]}


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
            "",
            "<details><summary>Patch</summary>", "", "```diff", patch.rstrip(), "```", "</details>", ""]


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
        f"Would-have-caught: {b['state']}",
        "",
        "Does this match how it looked when the original change was written? Corrections welcome.",
        "",
        "_Draft generated by the Debug Assist walking skeleton; placeholder content._",
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
    can_stop = {"read_issue", "reproduce", "find_cause", "write_fix"}  # the steps with typed early exits
    for a, b in zip(steps, steps[1:]):
        if a.__name__ in can_stop:
            g.add_conditional_edges(a.__name__, _unless_stopped(b.__name__), [b.__name__, END])
        else:
            g.add_edge(a.__name__, b.__name__)
    g.add_conditional_edges("approval", _after_approval, ["open_pr", END])
    g.add_edge("open_pr", END)
    return g.compile(checkpointer=MongoDBSaver(client(), db_name=CFG.db_name))
