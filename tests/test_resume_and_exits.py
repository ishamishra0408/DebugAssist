"""Resume after a crash, typed early exits, and the single door to the generation model."""
import dataclasses
import re
from pathlib import Path

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
    monkeypatch.setattr(graph, "CFG", dataclasses.replace(graph.CFG, runs_dir=tmp_path))
    monkeypatch.setattr(graph, "TEST_WRITER_READY", True)
    return {"run_id": "r1", "profile": {"image": "img", "language": "typescript"}, "triage": {"has_repro_p": 0.9}}


def test_reproduce_stops_never_reproduced_and_records_every_attempt(scratch_db, tmp_path, monkeypatch):
    s = _repro_state(tmp_path, monkeypatch)
    monkeypatch.setattr(graph, "_write_and_run_test",
                        lambda s, rung, n, h: ladder.Attempt(rung.name, n, ladder.ERROR, "SyntaxError"))
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
                        lambda s, rung, n, h: made.append((rung.name, n)) or ladder.Attempt(rung.name, n, ladder.RED, "AssertionError"))
    with events.bind("r1", "reproduce"):
        out = graph.reproduce(s)
    assert made == [("end_to_end", 3)]  # integration has no recorded fake server yet, so it is skipped
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
    for name in ["read_issue", "reproduce", "find_cause", "write_fix", "why_it_shipped", "lasting_guard",
                 "test_past_bugs", "open_pr"]:
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
    assert calls["read_issue"] == 1 and calls["reproduce"] == 1, "finished steps must not re-run"
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
