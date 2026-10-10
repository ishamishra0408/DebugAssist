"""Every run on the latest main (Isha 2026-10-10): fetch at the start; install only if the package list changed;
otherwise build only what changed and what uses it; say on the page which main the run is on."""
import dataclasses
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from debug_assist import checkout, fresh, graph, sandbox_e2b, viewer
from debug_assist.profiles import PROFILES
from test_viewer import _data

PROF = dataclasses.replace(PROFILES["vercel/ai"], filters="--filter 'pkg-b...' --filter 'pkg-c...'")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def _pkg(repo, name, deps=(), build=True):
    d = repo / "packages" / name
    (d / "src").mkdir(parents=True, exist_ok=True)
    (d / "package.json").write_text(json.dumps({"name": name, "scripts": {"build": "tsc"} if build else {},
                                                "dependencies": {x: "workspace:*" for x in deps}}))
    (d / "src" / "index.ts").write_text(f"export const v = '{name}';\n")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _pkg(r, "pkg-a")
    _pkg(r, "pkg-b", deps=["pkg-a"])
    _pkg(r, "pkg-c")
    _pkg(r, "pkg-d", deps=["pkg-a"])          # uses pkg-a, but the test machine never installed it
    (r / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n")
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "T")
    return r


def _commit(repo, msg):
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg)
    return _git(repo, "rev-parse", "HEAD")


def test_only_what_changed_and_what_uses_it_is_built(repo):
    t = _git(repo, "rev-parse", "HEAD")
    (repo / "packages/pkg-a/src/index.ts").write_text("export const v = 'changed';\n")
    m = _commit(repo, "M")
    p = fresh.plan(PROF, repo, t, m)
    assert p["lock_changed"] is False and p["files"] == 1
    assert p["build"] == ["pkg-a", "pkg-b"]                       # pkg-b uses pkg-a; pkg-d isn't installed; pkg-c untouched
    assert "--filter 'pkg-a'" in p["build_cmd"] and "--filter 'pkg-b'" in p["build_cmd"] and "pkg-d" not in p["build_cmd"]
    (repo / "pnpm-lock.yaml").write_text("lockfileVersion: 9\npackages: {}\n")
    m2 = _commit(repo, "M2")
    p2 = fresh.plan(PROF, repo, m, m2)
    assert p2["lock_changed"] is True and p2["build_cmd"] == ""   # the package list changed: install, not just build
    assert fresh.plan(PROF, repo, m2, m2) == {"files": 0, "lock_changed": False, "build": [], "build_cmd": ""}


def test_a_copy_moves_to_main_and_the_hosted_sandbox_gets_every_file_that_differs(repo, monkeypatch):
    t = _git(repo, "rev-parse", "HEAD")
    (repo / "packages/pkg-c/src/index.ts").write_text("export const v = 'new on main';\n")
    (repo / "packages/pkg-a/src/old.ts").write_text("x\n")
    _commit(repo, "M0")
    (repo / "packages/pkg-a/src/old.ts").unlink()
    m = _commit(repo, "M")
    _git(repo, "checkout", "-q", t)
    import debug_assist.config as config
    monkeypatch.setattr(config, "CFG", dataclasses.replace(config.CFG, sandbox_backend="e2b"))
    code = {"commit": m, "built_from": t, "build_cmd": "pnpm --filter 'pkg-c' build"}
    checkout.on_commit(PROF, repo, code)
    assert _git(repo, "rev-parse", "HEAD") == m
    assert (repo / ".git/da-built-from").read_text() == t and "pkg-c" in (repo / ".git/da-build-cmd").read_text()
    (repo / "packages/pkg-b/src/new.test.ts").write_text("it('x')\n")      # the run's own change
    got = sandbox_e2b.changed_files(repo)
    assert {"packages/pkg-c/src/index.ts", "packages/pkg-b/src/new.test.ts"} <= got
    assert "packages/pkg-a/src/old.ts" not in got or not (repo / "packages/pkg-a/src/old.ts").exists()


def test_the_run_starts_on_mains_newest_commit_and_rebuilds_the_machine_when_packages_changed(repo, tmp_path, monkeypatch, scratch_db):
    t = _git(repo, "rev-parse", "HEAD")
    (repo / "pnpm-lock.yaml").write_text("lockfileVersion: 9\npackages: {}\n")
    m = _commit(repo, "M")
    _git(repo, "checkout", "-q", t)
    import debug_assist.config as config
    monkeypatch.setattr(graph, "CFG", dataclasses.replace(graph.CFG, runs_dir=tmp_path, sandbox_backend="e2b"))
    monkeypatch.setattr(config, "CFG", dataclasses.replace(config.CFG, sandbox_backend="e2b"))
    monkeypatch.setattr(fresh, "latest", lambda p: {"commit": m, "branch": "main", "committed_at": "", "fetched_at": "2026-10-10T18:40:00+00:00"})
    monkeypatch.setattr(fresh, "machine", lambda p: t)
    recorded, built = [], []
    monkeypatch.setattr(fresh, "record_machine", lambda repo_, c: recorded.append(c))
    monkeypatch.setattr("debug_assist.pkgcheck.rebuild", lambda prof, co, log=print: built.append(prof.base_commit))
    monkeypatch.setattr(graph, "run_copy", lambda prof, dest, code=None: repo)
    from debug_assist import events
    with events.bind("r1", "gather_context"):
        code = graph._fresh_code({"run_id": "r1"}, PROF)
    assert code["commit"] == m and code["built_from"] == m and code.get("rebuilt") and built == [m] and recorded == [m]
    assert _git(repo, "rev-parse", "HEAD") == m


def test_the_page_says_which_main_the_run_is_on():
    code = {"commit": "ba05943" + "0" * 33, "branch": "main", "fetched_at": "2026-10-10T18:40:00+00:00", "built_from": "x", "build": ["ai"]}
    st = {**_data()["state"], "profile": {"repo": "vercel/ai"}, "code": code}
    page = viewer.render(_data(state=st), mode="live", token="t")
    assert "Branched from <b>main</b> at" in page and ">ba05943</code>" in page and "fetched 10 Oct, 18:40 UTC" in page
    assert 'href="https://github.com/vercel/ai/commit/ba05943' in page and "1 changed package rebuilt" in page
    assert "branched from main at ba05943" in page                                         # in the pull request too
    old = viewer.render(_data(), mode="live", token="t")
    assert "On the saved copy of the code" in old
    err = {**_data()["state"], "code": {"commit": "", "error": "could not read the latest main (timeout); the saved copy was used"}}
    assert "could not read the latest main" in viewer.render(_data(state=err), mode="live", token="t")


def test_the_pull_request_branches_from_that_main(tmp_path, monkeypatch):
    monkeypatch.setattr(graph, "CFG", dataclasses.replace(graph.CFG, runs_dir=tmp_path))
    monkeypatch.setattr(graph, "verify_approval", lambda *a: None)
    m = "ba05943" + "0" * 33
    s = {"run_id": "r2", "issue": {"number": 7, "owner": "vercel", "repo": "ai"}, "pr_body": "x",
         "code": {"commit": m, "fetched_at": "2026-10-10T18:40:00+00:00"}}
    (tmp_path / "r2").mkdir()
    graph.open_pr(s)
    script = (tmp_path / "r2" / "publish.sh").read_text()
    assert f"echo git fetch origin {m}" in script and f"echo git switch -c debugassist/fix-7 {m}" in script
    assert "Branches from main at ba05943, fetched 2026-10-10T18:40:00+00:00" in script
