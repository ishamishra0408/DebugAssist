"""Run files in MongoDB: after a wiped disk, the run folder and its code copies come back exactly."""
import dataclasses
import shutil
import subprocess

from debug_assist import artifacts


def _repo(path):
    (path / "packages/p/src").mkdir(parents=True)
    (path / "packages/p/src/a.ts").write_text("base\n")
    for c in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b"]):
        subprocess.run(c, cwd=path, check=True)


def test_a_wiped_run_folder_comes_back(scratch_db, monkeypatch, tmp_path):
    base, runs = tmp_path / "base", tmp_path / "runs"
    base.mkdir(); _repo(base)
    monkeypatch.setenv("RUN_FILES_IN_DB", "1")
    monkeypatch.setattr(artifacts, "CFG", dataclasses.replace(artifacts.CFG, runs_dir=runs))
    monkeypatch.setattr(artifacts, "_coll", lambda: scratch_db["run_files"])
    monkeypatch.setattr(artifacts, "_profile_of", lambda rid: object())
    monkeypatch.setattr("debug_assist.checkout.run_copy", lambda prof, dest: shutil.copytree(base, dest) and dest)
    run = runs / "r1"
    shutil.copytree(base, run / "checkout")
    (run / "checkout/packages/p/src/a.ts").write_text("fixed\n")                 # the fix
    (run / "checkout/packages/p/src/da-repro.test.ts").write_text("test\n")     # the failing test
    (run / "PR.md").write_text("## Fix\n")
    (run / "writer").mkdir(); (run / "writer/draft-1.ts").write_text("draft\n")
    assert artifacts.save_run("r1") == 5                                        # 3 files + 2 changes + the copy marker
    assert artifacts.save_run("r1") == 0                                        # nothing changed: nothing written
    shutil.rmtree(runs)                                                          # the host restarts: disk wiped
    assert artifacts.run_ids() == ["r1"]
    artifacts.restore_run("r1")
    assert (run / "PR.md").read_text() == "## Fix\n" and (run / "writer/draft-1.ts").read_text() == "draft\n"
    assert (run / "checkout/packages/p/src/a.ts").read_text() == "fixed\n"
    assert (run / "checkout/packages/p/src/da-repro.test.ts").read_text() == "test\n"
    # the run reverts its fix: the saved change is forgotten, so a later restore gives the unmodified file
    (run / "checkout/packages/p/src/a.ts").write_text("base\n")
    artifacts.save_run("r1")
    shutil.rmtree(runs); artifacts.restore_run("r1")
    assert (run / "checkout/packages/p/src/a.ts").read_text() == "base\n"


def test_off_unless_asked(monkeypatch):
    monkeypatch.delenv("RUN_FILES_IN_DB", raising=False)
    assert not artifacts.enabled() and artifacts.run_ids() == []
