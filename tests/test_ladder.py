"""The reproduction ladder: climb on GREEN, retry on ERROR, stop on RED, give up after 4 as NEVER REPRODUCED."""
import pytest

from debug_assist import ladder
from debug_assist.ladder import ERROR, GREEN, NEVER_REPRODUCED, RED, REPRODUCED, RUNGS, Attempt, classify, climb, plan
from conftest import DOCKER_UP


def scripted(*outcomes):
    """An attempt function that returns the given outcomes in order and records what it was asked."""
    asked = []

    def attempt(rung, n, history):
        asked.append((rung.name, n, len(history)))
        return Attempt(rung=rung.name, n=n, outcome=outcomes[len(asked) - 1], evidence="…")
    return attempt, asked


def test_red_on_the_first_rung_stops_the_climb():
    fn, asked = scripted(RED)
    r = climb(list(RUNGS), fn)
    assert (r.status, r.rung, len(asked)) == (REPRODUCED, "unit", 1)


def test_green_climbs_to_the_next_rung():
    fn, asked = scripted(GREEN, RED)
    r = climb(list(RUNGS), fn)
    assert [a[0] for a in asked] == ["unit", "integration"] and r.rung == "integration"


def test_error_retries_the_same_rung_and_the_writer_sees_the_history():
    fn, asked = scripted(ERROR, RED)
    r = climb(list(RUNGS), fn)
    assert asked == [("unit", 1, 0), ("unit", 2, 1)] and r.status == REPRODUCED


def test_four_attempts_without_red_stops_never_reproduced():
    fn, asked = scripted(ERROR, ERROR, GREEN, ERROR, RED)  # the 5th would have been red: the cap stops it first
    r = climb(list(RUNGS), fn)
    assert r.status == NEVER_REPRODUCED and len(asked) == 4 and r.rung is None


def test_running_out_of_rungs_stops_never_reproduced():
    fn, asked = scripted(GREEN, GREEN, GREEN)
    r = climb(list(RUNGS), fn)
    assert r.status == NEVER_REPRODUCED and len(asked) == 3


def test_resume_counts_earlier_attempts_and_skips_rungs_already_green():
    before = [{"rung": "unit", "n": 1, "outcome": "ERROR", "evidence": "SyntaxError"},
              {"rung": "unit", "n": 2, "outcome": "GREEN", "evidence": "exit 0"}]
    fn, asked = scripted(GREEN, GREEN)
    r = climb(list(RUNGS), fn, already=before)
    assert asked == [("integration", 3, 2), ("end_to_end", 4, 3)]  # nothing repeated; cap still 4 in total
    assert r.status == NEVER_REPRODUCED and len(r.attempts) == 4


def test_a_red_found_before_a_crash_is_not_redone():
    fn, asked = scripted()
    r = climb(list(RUNGS), fn, already=[{"rung": "unit", "n": 1, "outcome": "RED", "evidence": "AssertionError"}])
    assert r.status == REPRODUCED and asked == []


def test_plan_skips_rungs_the_issue_cannot_use():
    rungs, skipped = plan(has_repro_p=0.2, has_recorded_fixtures=False)
    assert [r.name for r in rungs] == ["unit"]
    assert set(skipped) == {"integration", "end_to_end"} and "p=0.20" in skipped["end_to_end"]
    assert [r.name for r in plan(0.9, True)[0]] == ["unit", "integration", "end_to_end"]


@pytest.mark.parametrize("lang,code,out,want", [
    ("python", 1, "FAILED tests/test_x.py::test_slash - AssertionError: assert '/a' == '/a/'\n1 failed", RED),
    ("python", 1, "FAILED tests/test_x.py::test_parse - TypeError: 'NoneType' object is not subscriptable\n1 failed", RED),
    ("python", 2, "ERROR collecting tests/test_x.py\nImportError while importing test module", ERROR),
    ("python", 5, "no tests ran", ERROR),
    ("python", 0, "1 passed", GREEN),
    ("typescript", 1, " FAIL  src/x.test.ts > slash\nAssertionError: expected '/a' to be '/a/'\n Tests  1 failed", RED),
    ("typescript", 1, " FAIL  src/x.test.ts [ src/x.test.ts ]\nError: Transform failed with 1 error", ERROR),
    ("typescript", 1, "Error: Cannot find module './missing'", ERROR),
    ("typescript", 1, "segmentation fault", ERROR),  # unexplained failure is never a reproduction
    ("typescript", 124, "TIMEOUT after 600s; container da-1 killed", ERROR),
])
def test_classify(lang, code, out, want):
    assert classify(lang, code, out)[0] == want


@pytest.mark.skipif(not DOCKER_UP, reason="Docker not running")
def test_classify_reads_real_test_runs_in_the_node_sandbox(tmp_path):
    """Real `node --test` output from the pinned vercel/ai image, not hand-written strings."""
    from debug_assist.profiles import NODE_IMAGE
    from debug_assist.sandbox import run_in_sandbox
    (tmp_path / "red.test.mjs").write_text("import t from 'node:test'; import a from 'node:assert/strict';\n"
                                           "t('slash', () => a.equal('/a', '/a/'));\n")
    (tmp_path / "green.test.mjs").write_text("import t from 'node:test'; import a from 'node:assert/strict';\n"
                                             "t('slash', () => a.equal('/a/', '/a/'));\n")
    (tmp_path / "broken.test.mjs").write_text("import t from 'node:test';\nt('slash', () => { a.equal( });\n")
    got = {}
    for name in ("red", "green", "broken"):
        r = run_in_sandbox(f"node --test {name}.test.mjs 2>&1", tmp_path, image=NODE_IMAGE, timeout=60)
        got[name] = classify("typescript", r.returncode, r.stdout + r.stderr)[0]
    assert got == {"red": RED, "green": GREEN, "broken": ERROR}
