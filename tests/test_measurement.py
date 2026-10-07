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
