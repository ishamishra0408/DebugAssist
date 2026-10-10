"""Resume after a crash, typed early exits, and the single door to the generation model."""
import dataclasses
import re
from pathlib import Path

from types import SimpleNamespace

import pytest

from debug_assist import diffview, events, graph, ladder, meter
from conftest import TEST_DB

SRC = Path(__file__).resolve().parents[1] / "src" / "debug_assist"


def test_only_models_py_can_build_a_generation_client():
    users = [p.name for p in SRC.glob("*.py") if "ChatOpenRouter" in p.read_text()]
    assert users == ["models.py"]
    from debug_assist import models
    assert not hasattr(models, "writer"), "the client builder must stay private (_writer)"
    callers = [p.name for p in SRC.glob("*.py") if p.name != "models.py" and re.search(r"\b_writer\(", p.read_text())]
    assert callers == []


# ── typed early exits in read_issue ──────────────────────────────────────────────────────────────
def _read_issue_with(monkeypatch, is_defect_p):
    monkeypatch.setattr(graph, "get_issue", lambda url: {"title": "t", "body": "b", "owner": "vercel", "repo": "ai",
                                                         "number": 1, "reporter": "x"})
    monkeypatch.setattr(graph, "decide", lambda text, q, which="general": (
        {"is_defect": {"noul": is_defect_p}, "kind": {"choice": "bug", "answer_confidence": 0.9}} if which == "triage"
        else {"has_repro": {"noul": 0.8}, "regression": {"noul": 0.1}}))
    return graph.read_issue({"issue_url": "https://github.com/vercel/ai/issues/1", "run_id": "r"})


def test_low_confidence_triage_stops_for_a_person(monkeypatch):
    out = _read_issue_with(monkeypatch, 0.55)
    assert out["outcome"]["exit"] == "NEEDS PERSON"


def test_confident_not_a_bug_stops(monkeypatch):
    assert _read_issue_with(monkeypatch, 0.08)["outcome"]["exit"] == "NOT A DEFECT"


def test_confident_bug_goes_on(monkeypatch):
    assert "outcome" not in _read_issue_with(monkeypatch, 0.97)


# ── the ladder inside the reproduce step ─────────────────────────────────────────────────────────
def _repro_state(tmp_path, monkeypatch):
    monkeypatch.setattr(graph, "secrets_visible", lambda work, image: [])
    monkeypatch.setattr(graph, "_existing_tests", lambda *a: {"status": "NONE FAIL", "passed": 12, "failed": 0})
    monkeypatch.setattr(graph, "CFG", dataclasses.replace(graph.CFG, runs_dir=tmp_path))
    monkeypatch.setattr(graph, "run_copy", lambda prof, dest, code=None: dest)
    monkeypatch.setattr(graph.testwriter, "locate", lambda *a: SimpleNamespace(
        source="packages/x/src/y.ts", fixtures=["packages/x/src/__fixtures__/a.chunks.txt"]))  # recorded data beside it
    return {"run_id": "r1", "issue": {"number": 1, "title": "t", "body": "b"},
            "profile": {"repo": "vercel/ai", "image": "img", "language": "typescript", "recorded_fixtures": True},
            "triage": {"has_repro_p": 0.9}}


def test_reproduce_stops_never_reproduced_and_records_every_attempt(scratch_db, tmp_path, monkeypatch):
    s = _repro_state(tmp_path, monkeypatch)
    monkeypatch.setattr(graph, "_write_and_run_test",
                        lambda s, rung, n, h, *ctx: ladder.Attempt(rung.name, n, ladder.ERROR, "SyntaxError"))
    with events.bind("r1", "reproduce"):
        out = graph.reproduce(s)
    assert out["outcome"]["exit"] == "NEVER REPRODUCED" and len(out["attempts"]) == 4
    assert graph._unless_stopped("find_cause")(out) == graph.END
    assert [e["kind"] for e in events.for_run("r1")] == ["attempt"] * 4  # written as they happened


def test_reproduce_after_a_crash_does_not_repeat_attempts(scratch_db, tmp_path, monkeypatch):
    s = _repro_state(tmp_path, monkeypatch)
    with events.bind("r1", "reproduce"):  # two attempts were logged, then the process died mid-step
        for n, o in ((1, "ERROR"), (2, "GREEN")):
            events.log("attempt", rung="unit", n=n, outcome=o, evidence="…", test_path="", at="")
    made = []
    monkeypatch.setattr(graph, "_write_and_run_test",
                        lambda s, rung, n, h, *ctx: made.append((rung.name, n)) or ladder.Attempt(rung.name, n, ladder.RED, "AssertionError"))
    with events.bind("r1", "reproduce"):
        out = graph.reproduce(s)
    assert made == [("integration", 3)]  # unit already went GREEN; the recorded-stream rung is next
    assert out["repro"]["status"] == "REPRODUCED" and out["repro"]["attempts_used"] == 3 and "outcome" not in out


# ── resume through the real graph and the MongoDB checkpointer ───────────────────────────────────
def _fake_steps(monkeypatch, calls, crash_once):
    def make(name, update=None):
        def fn(s):
            calls[name] = calls.get(name, 0) + 1
            if name == crash_once and calls[name] == 1:
                raise RuntimeError("simulated crash (laptop slept, provider died...)")
            return {"log": [name], **(update or {})}
        fn.__name__ = name
        return fn
    for name in ["read_issue", "gather_context", "reproduce", "find_cause", "write_fix", "why_it_shipped",
                 "lasting_guard", "test_past_bugs", "open_pr"]:
        monkeypatch.setattr(graph, name, make(name))
    monkeypatch.setattr(graph, "approval", make("approval", {"approval": {"status": "PENDING"}}))
    monkeypatch.setattr(graph, "CFG", dataclasses.replace(graph.CFG, db_name=TEST_DB))


def test_resume_continues_after_the_last_finished_step(scratch_db, monkeypatch):
    calls = {}
    _fake_steps(monkeypatch, calls, crash_once="find_cause")
    app, cfg = graph.build(), {"configurable": {"thread_id": "resume-test"}}
    with pytest.raises(RuntimeError, match="simulated crash"):
        app.invoke({"run_id": "resume-test", "issue_url": "u", "log": []}, cfg)
    assert app.get_state(cfg).next == ("find_cause",)
    app.invoke(None, cfg)  # what `debug-assist resume` does
    assert calls["read_issue"] == 1 and calls["gather_context"] == 1 and calls["reproduce"] == 1, \
        "finished steps must not re-run"
    assert calls["find_cause"] == 2 and calls["approval"] == 1


def test_a_typed_exit_ends_the_graph(scratch_db, monkeypatch):
    calls = {}
    _fake_steps(monkeypatch, calls, crash_once=None)
    monkeypatch.setattr(graph, "read_issue", lambda s: {"outcome": graph.stop("NEEDS PERSON", "low confidence"),
                                                        "log": ["read_issue"]})
    graph.read_issue.__name__ = "read_issue"
    app, cfg = graph.build(), {"configurable": {"thread_id": "exit-test"}}
    final = app.invoke({"run_id": "exit-test", "issue_url": "u", "log": []}, cfg)
    assert final["outcome"]["exit"] == "NEEDS PERSON" and "reproduce" not in calls


def test_focus_comes_from_the_issue_section_or_is_given():
    issue = {"body": "### Description\nGateway drops message.\n\n### Secondary observation\n\nFlush emits a half call.\n\n### Related\n#1"}
    assert graph.focus_of(issue, None, "Secondary observation") == "Flush emits a half call."
    assert graph.focus_of(issue, "given", "Secondary observation") == "given"
    assert graph.focus_of(issue, None, "Not there") == ""
    told = {"body": "`useChat` keeps old messages.\n\n### Reproduction\npnpm add"}   # #22543: told before any heading
    assert graph.focus_of(told, None, "(description)") == "`useChat` keeps old messages."


def test_a_missing_focus_section_stops_for_a_person(monkeypatch):
    monkeypatch.setattr(graph, "get_issue", lambda url: {"title": "t", "body": "### A\nx", "owner": "vercel",
                                                         "repo": "ai", "number": 1, "reporter": "x"})
    monkeypatch.setattr(graph, "decide", lambda text, q, which="general": (
        {"is_defect": {"noul": 0.97}, "kind": {"choice": "bug", "answer_confidence": 0.9}} if which == "triage"
        else {"has_repro": {"noul": 0.8}, "regression": {"noul": 0.1}}))
    out = graph.read_issue({"issue_url": "u", "run_id": "r", "focus_heading": "Secondary observation"})
    assert out["outcome"]["exit"] == "NEEDS PERSON" and "Secondary observation" in out["outcome"]["why"]


def test_the_pr_text_says_how_it_was_reproduced_and_whether_it_was_confirmed():
    r = {"status": "REPRODUCED", "rung": "unit", "failing_test": "packages/p/src/da-repro-1-unit-1.test.ts",
         "evidence": "❯ x (1 failed)\nAssertionError: expected [ { type: 'tool-call' } ] to strictly equal []",
         "confirmed": False, "confirmed_by": "integration", "attempts_used": 2}
    text = "\n".join(graph.reproduction_lines(r))
    assert "not observed live" in text and "NOT confirmed" in text and "AssertionError" in text
    assert graph.reproduction_lines({"status": "NEVER REPRODUCED"}) == []


def test_a_confirmation_that_never_gave_a_valid_result_is_said_as_such():
    r = {"status": "REPRODUCED", "rung": "unit", "failing_test": "t", "evidence": "AssertionError: x",
         "confirmed": None, "confirm_tries": 2, "attempts_used": 4}
    assert "tried 2× without a valid result" in "\n".join(graph.reproduction_lines(r))


def test_only_the_judging_test_stays_in_the_code(tmp_path):
    co, rd = tmp_path / "co", tmp_path / "run"
    for name in ("da-repro-1-unit-1.test.ts", "da-repro-1-unit-2.test.ts", "da-repro-1-integration-3.test.ts"):
        (co / "packages/p/src").mkdir(parents=True, exist_ok=True)
        (co / "packages/p/src" / name).write_text("x")
    paths = [f"packages/p/src/da-repro-1-{n}.test.ts" for n in ("unit-1", "unit-2", "integration-3")] + [""]
    moved = graph.shelve_drafts(co, rd, paths, keep="packages/p/src/da-repro-1-integration-3.test.ts")
    assert len(moved) == 2 and (co / "packages/p/src/da-repro-1-integration-3.test.ts").exists()
    assert sorted(p.name for p in (rd / "attempt-tests").iterdir()) == ["da-repro-1-unit-1.test.ts", "da-repro-1-unit-2.test.ts"]


@pytest.mark.parametrize("holdout,validated,judges,stopped", [
    ({"status": "PASSED", "test": "t2"}, True, 2, False),
    ({"status": "INCONCLUSIVE", "test": None, "evidence": ""}, False, 1, False),  # reaches the PR, labelled one judge
])
def test_only_a_fix_confirmed_by_two_tests_counts_toward_the_fix_clock(tmp_path, monkeypatch, holdout, validated,
                                                                       judges, stopped):
    monkeypatch.setattr(graph, "CFG", dataclasses.replace(graph.CFG, runs_dir=tmp_path))
    monkeypatch.setattr(graph, "run_copy", lambda prof, dest, code=None: dest)
    monkeypatch.setattr(graph.testwriter, "locate", lambda *a: SimpleNamespace(source="packages/p/src/x.ts"))
    monkeypatch.setattr(graph.fixer, "_git", lambda *a: "")
    monkeypatch.setattr(graph.fixer, "write_fix", lambda *a, **k: {"status": "VALIDATED", "attempts": [{"evidence": ""}],
                                                                 "changed": ["packages/p/src/x.ts"], "patch": "diff",
                                                                 "suites": {"p": "pass"}})
    monkeypatch.setattr(graph.fixer, "holdout", lambda *a, **k: holdout)
    s = {"run_id": "r", "issue": {"number": 1, "title": "t", "body": "b"}, "focus": "f",
         "profile": {"repo": "vercel/ai"}, "fix_clock": {"started_at": graph.now()},
         "repro": {"checkout": str(tmp_path / "co"), "failing_test": "t1", "oracle_test": "t1"},
         "cause": {"file": "packages/p/src/x.ts", "lines": [1, 2], "why": "w", "looked_up": []}}
    out = graph.write_fix(s)
    assert out["fix_clock"]["validated"] is validated and out["fix_clock"]["judges"] == judges
    assert ("outcome" in out) is stopped
    if judges == 1:
        assert any("ONE JUDGE ONLY" in l for l in out["log"])


@pytest.mark.parametrize("third,judges", [({"status": "PASSED", "test": "t3"}, 2),
                                          ({"status": "INCONCLUSIVE", "test": None, "evidence": ""}, 1),
                                          ({"status": "FIX INCOMPLETE", "test": "t3", "evidence": "e"}, 1)])
def test_a_round_two_fix_needs_a_fresh_third_test_for_two_judges(tmp_path, monkeypatch, third, judges):
    """Ruled 2026-10-07 (north-star-v1.2): the round-2 fixer saw the second test fail, so that test is not blind."""
    monkeypatch.setattr(graph, "CFG", dataclasses.replace(graph.CFG, runs_dir=tmp_path))
    monkeypatch.setattr(graph, "run_copy", lambda prof, dest, code=None: dest)
    monkeypatch.setattr(graph.testwriter, "locate", lambda *a: SimpleNamespace(source="packages/p/src/x.ts"))
    monkeypatch.setattr(graph.fixer, "_git", lambda *a: "")
    monkeypatch.setattr(graph.fixer, "revert", lambda *a: None)
    monkeypatch.setattr(graph.fixer, "write_fix", lambda *a, **k: {"status": "VALIDATED", "attempts": [{"evidence": ""}],
                                                                 "changed": ["packages/p/src/x.ts"], "patch": "diff",
                                                                 "suites": {"p": "pass"}})
    calls = []

    def holdout(*a, **k):
        calls.append(k["drafts"].name)
        return {"status": "FIX INCOMPLETE", "test": "t2", "evidence": "e"} if len(calls) == 1 else third
    monkeypatch.setattr(graph.fixer, "holdout", holdout)
    s = {"run_id": "r", "issue": {"number": 1, "title": "t", "body": "b"}, "focus": "f",
         "profile": {"repo": "vercel/ai"}, "fix_clock": {"started_at": graph.now()},
         "repro": {"checkout": str(tmp_path / "co"), "failing_test": "t1", "oracle_test": "t1"},
         "cause": {"file": "packages/p/src/x.ts", "lines": [1, 2], "why": "w", "looked_up": []}}
    out = graph.write_fix(s)
    assert calls == ["holdout", "holdout3"]
    assert out["fix_clock"]["judges"] == judges and out["fix_clock"]["validated"] is (judges == 2)


def test_judge_count_reads_old_round_two_runs_as_one_judge():
    from debug_assist.fixer import judge_count, THIRD_TEST_PASSED
    assert judge_count("VALIDATED", {"status": "PASSED"}) == 2
    assert judge_count("VALIDATED", {"status": THIRD_TEST_PASSED}) == 2
    assert judge_count("VALIDATED", {"status": "PASSED (round 2)"}) == 1   # before the ruling: the fix saw the test
    assert judge_count("NOT VALIDATED", {"status": "PASSED"}) == 0


def test_the_repos_own_tests_run_first_and_each_repro_test_runs_again_after_the_fix(tmp_path, monkeypatch):
    """Proof, both ways (Isha 2026-10-08): does a test already in the repo fail for this issue (once, then reused on
    resume); and after the fix, does each test that showed the bug pass (a shelved one is put back for the run)."""
    import subprocess
    from types import SimpleNamespace
    from debug_assist import events, testwriter
    from debug_assist.profiles import PROFILES
    monkeypatch.setattr(graph, "CFG", dataclasses.replace(graph.CFG, runs_dir=tmp_path))
    logged = []
    monkeypatch.setattr(events, "log", lambda kind, key="", **kw: logged.append({"kind": kind, **kw}))
    monkeypatch.setattr(events, "for_run", lambda rid: logged)
    ran = []
    out = (" FAIL  src/a.test.ts > stream > ends\nAssertionError: expected [ { type: 'tool-call' } ] to equal []\n"
           "+ { type: 'tool-call' }\n Tests  1 failed | 411 passed (412)\n")
    monkeypatch.setattr("debug_assist.sandbox.run_in_sandbox",
                        lambda cmd, wd, **k: (ran.append(cmd), subprocess.CompletedProcess(cmd, 1 if not ran[1:] else 0, out, ""))[1])
    s = {"run_id": "ai-1-x", "issue": {"title": "t"}, "focus": "emits a `tool-call` part"}
    ctx = SimpleNamespace(source="packages/openai-compatible/src/chat/x.ts", package_dir="packages/openai-compatible")
    got = graph._existing_tests(s, tmp_path, ctx, PROFILES["vercel/ai"])
    assert got["status"] == "FOUND" and got["passed"] == 411 and got["failed"] == 1 and got["package"] == "packages/openai-compatible"
    assert ran[0].endswith("cd packages/openai-compatible && pnpm test:node") and (tmp_path / "ai-1-x/proof/existing-tests.txt").exists()
    assert graph._existing_tests(s, tmp_path, ctx, PROFILES["vercel/ai"])["status"] == "FOUND" and len(ran) == 1  # reused
    (tmp_path / "ai-1-x/attempt-tests").mkdir(parents=True)
    (tmp_path / "ai-1-x/attempt-tests/da-repro-1-unit-2.test.ts").write_text("shelved")
    (tmp_path / "co/packages/openai-compatible/src").mkdir(parents=True)
    res = graph._after_fix(s, PROFILES["vercel/ai"], tmp_path / "co", ["packages/openai-compatible/src/da-repro-1-unit-2.test.ts", None])
    assert res[0]["outcome"] == "GREEN" and "da-repro-1-unit-2.test.ts" in ran[-1]
    assert not (tmp_path / "co/packages/openai-compatible/src/da-repro-1-unit-2.test.ts").exists()       # taken out again
    proof = (tmp_path / "ai-1-x/proof/after-fix-da-repro-1-unit-2.test.ts.txt").read_text()
    assert "with the fix" in proof and "source unchanged" not in proof and "unfixed" not in proof   # review of #22543
    assert res[0]["line"] == "exit 0: the test passed with the fix"
    assert testwriter.counts("====== 2 failed, 40 passed in 3.1s ======") == {"passed": 40, "failed": 2}
    assert testwriter.counts("# pass 7\n# fail 0") == {"passed": 7, "failed": 0} and testwriter.counts("nothing") == {}


def test_no_recorded_data_beside_the_code_means_no_recorded_data_rung(scratch_db, tmp_path, monkeypatch):
    """#22288 run: vercel/ai has recordings for its providers, none beside its chat code; three tries were spent
    looking for them. The rung is planned per issue now."""
    s = _repro_state(tmp_path, monkeypatch)
    monkeypatch.setattr(graph.testwriter, "locate", lambda *a: SimpleNamespace(source="packages/ai/src/ui/chat.ts", fixtures=[]))
    made = []
    monkeypatch.setattr(graph, "_write_and_run_test", lambda s, r, n, h, ctx, co: (
        made.append(r.name), ladder.Attempt(rung=r.name, n=n, outcome=ladder.RED, evidence="AssertionError", test_path="t"))[1])
    out = graph.reproduce(s)
    assert made == ["unit"] and out["repro"]["status"] == ladder.REPRODUCED
    assert out["repro"]["ladder_plan"]["skipped"]["integration"] == "no recorded data beside packages/ai/src/ui"


def test_the_pr_is_written_the_way_github_prs_are(monkeypatch):
    """Isha 2026-10-08: a commit message (recommended, yours to edit), the change with its tests, and a description in
    GitHub's shape. Approving the text approves the change: its fingerprint closes the description."""
    from debug_assist import diffview
    from debug_assist.guardrails import fingerprint
    patch = ("diff --git a/packages/ai/src/ui/chat.ts b/packages/ai/src/ui/chat.ts\n--- a/packages/ai/src/ui/chat.ts\n"
             "+++ b/packages/ai/src/ui/chat.ts\n@@ -1,1 +1,1 @@\n-a\n+b\n"
             + diffview.new_file_diff("packages/ai/src/ui/da-repro-9-unit-1.test.ts", "it('x')")
             + diffview.new_file_diff("packages/ai/src/ui/da-repro-9-holdout-1.test.ts", "it('y')"))
    s = {"run_id": "r", "issue": {"number": 9, "owner": "vercel", "repo": "ai", "title": "Chat.resumeStream duplicates text parts"},
         "profile": {"repo": "vercel/ai"},
         "cause": {"file": "packages/ai/src/ui/chat.ts", "lines": [883, 897], "why": "The resume reuses the retained state",
                   "plan": "Continue the retained text part when a resumed stream replays text-start. Keep other paths."},
         "repro": {"failing_test": "packages/ai/src/ui/da-repro-9-unit-1.test.ts", "evidence": "AssertionError: expected two parts",
                   "existing_tests": {"passed": 4252, "package": "packages/ai"},
                   "ladder_plan": {"skipped": {"integration": "no recorded data beside packages/ai/src/ui", "end_to_end": "needs keys"}}},
         "fix": {"status": "VALIDATED", "suites": ["packages/ai"], "holdout": {"status": "PASSED", "test": "packages/ai/src/ui/da-repro-9-holdout-1.test.ts"}},
         "guard": {"text": "A test over every way a resume replays parts.", "open_cases": [], "siblings": []},
         "backtest": {"state": "NONE"}, "condition": {"text": "Resume reused state no test covered."},
         "second_story": {"text": "### What broke?\nx"}}
    msg = graph.commit_message(s)
    assert msg == ("fix(ai): continue the retained text part when a resumed stream replays\n\nThe resume reuses the retained "
                   "state\n\nFixes #9\n") or msg.startswith("fix(ai): continue the retained text part")
    assert len(msg.splitlines()[0]) <= 72 and msg.rstrip().endswith("Fixes #9")
    body = graph.compose_pr_body(s, patch)
    heads = [l for l in body.splitlines() if l.startswith("## ")]
    assert heads == ["## Summary", "## Changes", "## Tests", "## Why this slipped through", "## Follow-ups (not in this PR)"]
    assert "Fixes #9" in body and "- `packages/ai/src/ui/chat.ts` (+1 −1)" in body and "```diff" not in body   # no pasted patch
    assert "**Unit test**, added `packages/ai/src/ui/chat.issue-9.test.ts`: fails on `main` with `AssertionError: expected two parts`" in body
    assert "a second test of the same problem, written without seeing this change" in body
    assert "**Integration test**: none added. There is no recorded real data beside this code" in body
    assert "**Automation (end-to-end) test**: none added. It would need live provider keys" in body
    assert "still pass in packages/ai (4252 tests in packages/ai before the change)" in body
    assert body.rstrip().endswith(f"<!-- debugassist: change sha256 {fingerprint(patch)} -->")


def test_the_pr_carries_the_fix_one_test_named_for_its_file_and_a_changeset(tmp_path):
    """Review of run #22543 (2026-10-10): two near-identical run tests in src/ and no changeset; vercel/ai asks for one."""
    import subprocess
    co = tmp_path / "checkout"
    (co / "packages/vue/src").mkdir(parents=True)
    (co / ".changeset").mkdir()
    (co / ".changeset/config.json").write_text("{}")
    (co / "packages/vue/package.json").write_text('{"name": "@ai-sdk/vue"}')
    (co / "packages/vue/src/use-object.ts").write_text("a\n")
    subprocess.run(["git", "init", "-q"], cwd=co, check=True)
    subprocess.run(["git", "add", "-A"], cwd=co, check=True)
    subprocess.run(["git", "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "base"], cwd=co, check=True)
    (co / "packages/vue/src/use-object.ts").write_text("b\n")
    (co / "packages/vue/src/da-repro-22543-unit-2.ui.test.ts").write_text("it('judge')\n")
    (co / "packages/vue/src/da-repro-22543-holdout-2.ui.test.ts").write_text("it('second')\n")
    s = {"run_id": "r", "issue": {"number": 22543, "owner": "vercel", "repo": "ai", "title": "t"}, "profile": {"repo": "vercel/ai"},
         "cause": {"file": "packages/vue/src/use-object.ts", "plan": "Normalize the headers. More."},
         "repro": {"checkout": str(co), "oracle_test": "packages/vue/src/da-repro-22543-unit-2.ui.test.ts"},
         "fix": {"holdout": {"status": "PASSED", "test": "packages/vue/src/da-repro-22543-holdout-2.ui.test.ts"}}}
    patch = graph._pr_change(s)
    paths = [x["path"] for x in diffview.parse(patch)]
    assert paths == ["packages/vue/src/use-object.ts", "packages/vue/src/use-object.issue-22543.ui.test.ts",
                     ".changeset/debugassist-fix-22543.md"]
    assert "+'@ai-sdk/vue': patch" in patch and "+fix(vue): normalize the headers" in patch
    body = graph.compose_pr_body({**s, "guard": {"text": "g", "open_cases": [], "siblings": []}, "backtest": {"state": "NONE"},
                                  "condition": {"text": "c"}, "second_story": {"text": "x"}}, patch)
    assert "- `packages/vue/src/use-object.ts` (+1 −1)" in body and ".changeset/" not in body.split("## Tests")[0]
    assert "added `packages/vue/src/use-object.issue-22543.ui.test.ts`" in body
    assert "**Also checked, kept with the run** (not added here): `da-repro-22543-holdout-2.ui.test.ts`" in body
    assert "**Changeset**" in body


def test_a_package_whose_own_tests_cannot_load_stops_before_any_test_is_written(scratch_db, tmp_path, monkeypatch):
    """#22085 run, 2026-10-08: @ai-sdk/workflow was not installed on the test machine; every test file failed on a
    missing config, and 4 Opus tries were then spent on tests that could never run. Now it stops at once, for free."""
    import subprocess
    from debug_assist.profiles import PROFILES
    out = (" FAIL  src/do-generate-step.test.ts [ src/do-generate-step.test.ts ]\n"
           "Error: Cannot find module './node_modules/@vercel/ai-tsconfig/ts-library.json'\n"
           " Test Files  30 failed (30)\n      Tests  no tests\n")
    monkeypatch.setattr("debug_assist.sandbox.run_in_sandbox", lambda cmd, wd, **k: subprocess.CompletedProcess(cmd, 1, out, ""))
    s = {"run_id": "ai-22085-x", "issue": {"title": "t"}, "focus": "keeps a `tool-call` without its result"}
    monkeypatch.setattr(graph, "CFG", dataclasses.replace(graph.CFG, runs_dir=tmp_path))
    ctx = SimpleNamespace(source="packages/workflow/src/model-call-iterator.ts", package_dir="packages/workflow")
    with events.bind("ai-22085-x", "reproduce"):
        got = graph._existing_tests(s, tmp_path, ctx, PROFILES["vercel/ai"])
    assert got["status"] == "CANNOT RUN" and "Cannot find module" in got["why"]
    s = _repro_state(tmp_path, monkeypatch)
    monkeypatch.setattr(graph, "_existing_tests", lambda *a: got)
    wrote = []
    monkeypatch.setattr(graph, "_write_and_run_test", lambda *a: wrote.append(a))
    with events.bind("r1", "reproduce"):
        res = graph.reproduce(s)
    assert res["outcome"]["exit"] == "TEST MACHINE NOT READY" and not wrote and res["repro"]["attempts_used"] == 0
    assert "packages/workflow" in res["outcome"]["why"] and graph._unless_stopped("find_cause")(res) == graph.END


def test_the_repos_own_failing_tests_say_cant_tell_when_the_issue_quotes_no_code(scratch_db, tmp_path, monkeypatch):
    """Review of run #22085 (2026-10-09): with nothing to check against, a failure was silently "not for this issue"."""
    import subprocess
    from debug_assist.profiles import PROFILES
    out = (" FAIL  src/a.test.ts > a > b\nAssertionError: expected 1 to be 2\n"
           " Test Files  1 failed | 17 passed (18)\n      Tests  1 failed | 332 passed (333)\n")
    monkeypatch.setattr("debug_assist.sandbox.run_in_sandbox", lambda cmd, wd, **k: subprocess.CompletedProcess(cmd, 1, out, ""))
    monkeypatch.setattr(graph, "CFG", dataclasses.replace(graph.CFG, runs_dir=tmp_path))
    s = {"run_id": "ai-22085-y", "issue": {"title": "t"}, "focus": "WorkflowAgent keeps a call without its result"}
    ctx = SimpleNamespace(source="packages/workflow/src/model-call-iterator.ts", package_dir="packages/workflow")
    with events.bind("ai-22085-y", "reproduce"):
        got = graph._existing_tests(s, tmp_path, ctx, PROFILES["vercel/ai"])
    assert got["status"] == "CAN'T TELL" and got["failed"] == 1 and got["passed"] == 332


def test_the_fix_clock_leaves_out_waiting_for_you_and_installing():
    """Review of run #22543 (F8): the clock counted 62 s waiting for the install answer and 149 s of installing."""
    evs = [{"kind": "step", "ended": "paused for approval", "at": "2026-10-10T19:21:15.000+00:00"},
           {"kind": "decision", "decision": "install yes", "at": "2026-10-10T19:22:17.000+00:00"},
           {"kind": "install", "answer": "yes", "at": "2026-10-10T19:22:18.000+00:00"},
           {"kind": "install", "seconds": 149, "at": "2026-10-10T19:24:47.000+00:00"},
           {"kind": "prepare", "what": "install", "seconds": 30.5, "at": "2026-10-10T19:26:00.000+00:00"},
           {"kind": "install", "seconds": 999, "at": "2026-10-10T20:00:00.000+00:00"}]          # after the clock stopped
    assert graph.time_away(evs, "2026-10-10T19:18:00+00:00", "2026-10-10T19:30:00+00:00") == {
        "waiting_for_you": 62.0, "installing": 179.5}
