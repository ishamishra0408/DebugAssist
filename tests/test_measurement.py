"""The measurement layer: one write per thing, runs vs trials, north stars computed from runs."""
import hashlib

import pytest

from debug_assist import events, meter, northstar
from debug_assist.config import NORTH_STAR_DOC, NORTH_STAR_SEAL


def test_a_keyed_event_is_written_once_even_when_a_step_reruns(scratch_db):
    with events.bind("r1", "write_fix"):
        events.log("fix_attempt", key="fixer#1", n=1, ok=False)
        events.log("fix_attempt", key="fixer#1", n=1, ok=False)   # the step re-ran after a crash
        events.log("fix_attempt", key="fixer-round2#1", n=1, ok=True)  # a second round is a different thing
        events.log("laya", model="general")                        # no natural key: written each time
    rows = events.for_run("r1")
    assert [r["kind"] for r in rows].count("fix_attempt") == 2 and len(rows) == 3


def test_trials_are_kept_apart_from_runs(scratch_db):
    meter.open_run("r1", 1.0, 600)
    meter.open_run("r1-refix", 0.1, 600, kind="trial", parent="r1")
    assert scratch_db["meters"].find_one({"_id": "r1"})["kind"] == "run"
    t = scratch_db["meters"].find_one({"_id": "r1-refix"})
    assert t["kind"] == "trial" and t["parent"] == "r1"
    with events.bind("r1-refix", "write_fix"):
        events.log("fix_attempt", key="fixer#1", n=1)
    assert events.trials_of("r1") == {"r1-refix": {"n": 1, "kinds": ["fix_attempt"], "last": events.for_run("r1-refix")[0]["at"]}}


def test_time_to_validated_fix_counts_only_two_judge_fixes_over_pickups():
    rows = [{"run_id": "a", "picked_up": True, "seconds": 2.0, "two_judges": False},
            {"run_id": "b", "picked_up": True, "seconds": 204.3, "two_judges": True},
            {"run_id": "c", "picked_up": True, "seconds": 130.2, "two_judges": True},
            {"run_id": "d", "picked_up": False, "seconds": None, "two_judges": False}]
    t = northstar.time_to_validated_fix(rows)
    assert (t["pickups"], t["reached"], t["seconds"], t["range"]) == (3, 2, [130.2, 204.3], (130.2, 204.3))


def test_would_have_caught_is_not_scored_without_siblings():
    assert northstar.would_have_caught([{"siblings": 0}, {"siblings": 0}])["state"].startswith("NOT SCORED")
    assert northstar.would_have_caught([{"siblings": 2}])["m"] == 2


@pytest.mark.skipif(not NORTH_STAR_DOC.exists(), reason="the design folder is not on this machine")
def test_the_north_star_definition_is_the_sealed_one():
    """Editing north-star-v1.1 without a new ruling fails here (sealed by Isha 2026-10-07)."""
    assert hashlib.sha256(NORTH_STAR_DOC.read_bytes()).hexdigest() == NORTH_STAR_SEAL


def test_skeleton_runs_and_each_model_are_reported_apart():
    rows = [{"run_id": "s", "model": "dev", "skeleton": True, "picked_up": True, "seconds": 1.0, "two_judges": False},
            {"run_id": "d", "model": "dev", "skeleton": False, "picked_up": True, "seconds": 9.0, "two_judges": False},
            {"run_id": "o", "model": "opus", "skeleton": False, "picked_up": True, "seconds": 130.2, "two_judges": True}]
    t = northstar.time_to_validated_fix(rows)
    assert (t["pickups"], t["reached"], t["skeleton"]) == (3, 1, 1)
    assert t["by_model"] == {"dev": {"reached": 0, "pickups": 1}, "opus": {"reached": 1, "pickups": 1}}


def test_m_zero_says_when_the_corpus_could_not_hold_a_sibling():
    """Independent grade 2026-10-07: the corpus held only #21439, so 'no past sibling' alone overstated the search."""
    w = northstar.would_have_caught([{"siblings": 0}], corpus=["https://github.com/vercel/ai/issues/21439"])
    assert w["state"].startswith("NOT SCORED: no past sibling (m = 0)") and "no search could have found" in w["state"]
    assert "no search" not in northstar.would_have_caught([{"siblings": 0}], corpus=["a", "b"])["state"]
    assert northstar.would_have_caught([{"siblings": 2}])["state"].startswith("NOT SCORED YET")  # k needs a non-author


def test_the_sibling_search_is_unevaluable_when_the_corpus_holds_no_other_issue(scratch_db, monkeypatch):
    from debug_assist import graph
    monkeypatch.setattr(graph, "CONDITIONS", scratch_db["conditions"])
    monkeypatch.setattr(graph, "verify_condition_frozen", lambda *a: None)
    monkeypatch.setattr(graph, "_backtest_guard", lambda s: None)
    monkeypatch.setattr(graph, "embedder", lambda: type("E", (), {"embed_query": lambda self, t: [0.1] * 4})())
    url = "https://github.com/vercel/ai/issues/21439"
    scratch_db["conditions"].insert_one({"run_id": "earlier", "issue_url": url, "text": "c", "embedding": [0.1] * 4})
    s = {"run_id": "r1", "issue_url": url, "condition": {"text": "a condition", "sha256": "x"},
         "guard": {"created_at": "t"}}
    bt = graph.test_past_bugs(s)["backtest"]
    assert bt["state"].startswith("UNEVALUABLE") and bt["other_issues"] == 0 and bt["candidates"] == []


def test_the_pr_body_never_reads_the_self_check_as_the_score():
    from debug_assist.graph import backtest_lines
    fa = {"window": 50, "fired": 0, "quiet": 0, "bug_already_there": 7, "unevaluable": 0, "not_run": 43, "groups_run": 7}
    text = "\n".join(backtest_lines({"state": "UNEVALUABLE (…)", "candidates": [],
                                     "detail": {"anchor": {"sha": "abc"}, "would_have_caught": True, "false_alarms": fa}}))
    assert "🎯 Would-have-caught: **NOT SCORED**" in text and "Self-check, not the 🎯 score" in text
    assert "would it have caught this bug" not in text
