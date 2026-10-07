"""The back-test: which commits share code for the guard, and how judge + guard results are read."""
from types import SimpleNamespace

from debug_assist import backtest

C = [{"sha": f"c{i}", "date": "d", "title": "t"} for i in range(6)]  # newest first; c0 is the anchor


def test_untouched_commits_carry_the_code_of_the_next_older_touching_commit():
    g = backtest.groups(C, [True, False, False, True, False, False])
    assert [x["rep"]["sha"] for x in g] == ["c0", "c3", "c5"]
    assert [[m["sha"] for m in x["members"]] for x in g] == [["c0"], ["c1", "c2", "c3"], ["c4", "c5"]]


def _profile():
    return SimpleNamespace(env="", build_cmd="b", image="n", language="typescript",
                           test_cmd="cd packages/{package} && pnpm test:node {test_path}")


def _hist(tmp_path):
    (tmp_path / "packages/p/src").mkdir(parents=True)
    (tmp_path / "packages/p/package.json").write_text('{"name": "@x/p"}')
    return tmp_path


def _run(judge_red, guard_red, monkeypatch):
    monkeypatch.setattr(backtest, "_git", lambda *a, **k: SimpleNamespace(returncode=0, stderr=""))
    red = "AssertionError: expected [ { type: 'tool-call' } ] to strictly equal []\n 1 failed"

    def run(cmd, workdir, network, timeout, image):
        if "judge" in cmd:
            return SimpleNamespace(returncode=1 if judge_red else 0, stdout=(" × j\n FAIL  f > j\n" + red) if judge_red else " ✓ j", stderr="")
        if "guard" in cmd:
            return SimpleNamespace(returncode=1 if guard_red else 0, stdout=(" × g\n FAIL  f > g\n" + red) if guard_red else " ✓ g", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return run


FOCUS = "flush emits a complete-looking `tool-call` part"


def test_reading_judge_and_guard_together(tmp_path, monkeypatch):
    hist = _hist(tmp_path)
    files = {"judge": ("packages/p/src/judge.test.ts", "x"), "guard": ("packages/p/src/guard.test.ts", "y")}
    for jr, gr, want in [(True, True, "CAUGHT"), (True, False, "MISSED"), (False, True, "FALSE ALARM"), (False, False, "QUIET")]:
        res = backtest.run_at(hist, "c1", _profile(), "p", files, FOCUS, _run(jr, gr, monkeypatch))
        assert res["state"] == want, (jr, gr, res)
    assert not (hist / "packages/p/src/judge.test.ts").exists()  # nothing left behind in the history clone


def test_a_commit_the_tests_cannot_run_on_is_unevaluable(tmp_path, monkeypatch):
    hist = _hist(tmp_path)
    monkeypatch.setattr(backtest, "_git", lambda *a, **k: SimpleNamespace(returncode=0, stderr=""))
    broken = lambda cmd, *a, **k: SimpleNamespace(returncode=1, stdout=" FAIL  f > j\nError: Cannot find module 'x'", stderr="") \
        if "test:node" in cmd else SimpleNamespace(returncode=0, stdout="", stderr="")
    files = {"judge": ("packages/p/src/judge.test.ts", "x"), "guard": ("packages/p/src/guard.test.ts", "y")}
    assert backtest.run_at(hist, "c1", _profile(), "p", files, FOCUS, broken)["state"] == "UNEVALUABLE"
    assert backtest.run_at(hist, "c1", _profile(), "missing", files, FOCUS, broken)["state"] == "UNEVALUABLE"
