"""Cleanup deletes only finished runs' code copies, and saves their changes first."""
import subprocess

from debug_assist import cleanup


def _run_with_copy(runs, name):
    co = runs / name / "checkout"
    co.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=co, check=True)
    (co / "src.ts").write_text("old\n")
    subprocess.run(["git", "add", "-A"], cwd=co, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"], cwd=co, check=True)
    (co / "src.ts").write_text("fixed\n")                       # a fix
    (co / "da-repro-1-unit-1.test.ts").write_text("expect(1)\n")  # a drafted test (untracked)
    return co


def test_dry_run_deletes_nothing(tmp_path):
    co = _run_with_copy(tmp_path, "done-run")
    rows = cleanup.run(lambda r: (True, "finished"), yes=False, runs_dir=tmp_path)
    assert rows[0]["delete"] and co.exists() and not (tmp_path / "done-run" / "checkout.patch").exists()


def test_only_finished_runs_lose_their_copy_and_their_changes_are_kept(tmp_path):
    done = _run_with_copy(tmp_path, "done-run")
    waiting = _run_with_copy(tmp_path, "paused-run")
    cleanup.run(lambda r: (r == "done-run", "x"), yes=True, runs_dir=tmp_path)
    assert not done.exists() and waiting.exists()
    patch = (tmp_path / "done-run" / "checkout.patch").read_text()
    assert "+fixed" in patch and "da-repro-1-unit-1.test.ts" in patch and "+expect(1)" in patch
