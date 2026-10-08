"""Resume after a crash, typed early exits, and the single door to the generation model."""
import dataclasses
import re
from pathlib import Path

from types import SimpleNamespace

import pytest

from debug_assist import events, graph, ladder, meter
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
    monkeypatch.setattr(graph, "run_copy", lambda prof, dest: dest)
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
    monkeypatch.setattr(graph, "run_copy", lambda prof, dest: dest)
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
    monkeypatch.setattr(graph, "run_copy", lambda prof, dest: dest)
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
    assert (tmp_path / "ai-1-x/proof/after-fix-da-repro-1-unit-2.test.ts.txt").exists()
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
