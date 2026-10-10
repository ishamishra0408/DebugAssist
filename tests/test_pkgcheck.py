"""Is the package the bug lives in installed on the test machine? If not, the run asks before any test is written
(Isha 2026-10-10, after #22085: the workflow package was not installed and the first run spent 4 tries for nothing)."""
import dataclasses
import json
import subprocess
from types import SimpleNamespace

import pytest

from debug_assist import graph, pkgcheck, profiles, server, viewer
from debug_assist.profiles import PROFILES
from test_viewer import _data, _sheet

VERCEL = PROFILES["vercel/ai"]


def _checkout(tmp_path, name="@ai-sdk/workflow"):
    (tmp_path / "packages/workflow").mkdir(parents=True)
    (tmp_path / "packages/workflow/package.json").write_text(json.dumps({"name": name}))
    return tmp_path


def test_the_test_machine_is_asked_whether_the_package_is_installed(tmp_path):
    co, asked = _checkout(tmp_path), []
    def run(said):
        return lambda cmd, wd, **k: (asked.append(cmd), subprocess.CompletedProcess(cmd, 0, said, ""))[1]
    no_wf = dataclasses.replace(VERCEL, filters=VERCEL.filters.replace(" --filter '@ai-sdk/workflow...'", ""))
    assert pkgcheck.missing(no_wf, co, "packages/workflow", run_cmd=run("MISSING\n")) == {"dir": "packages/workflow", "name": "@ai-sdk/workflow"}
    assert "test -d packages/workflow/node_modules" in asked[0]
    assert pkgcheck.missing(no_wf, co, "packages/workflow", run_cmd=run("INSTALLED\n")) is None
    asked.clear()
    assert pkgcheck.missing(VERCEL, co, "packages/workflow", run_cmd=run("MISSING\n")) is None and not asked   # on the list
    assert pkgcheck.missing(VERCEL, co, "packages/workflow", run_cmd=run("MISSING\n"), trust_list=False)       # checked anyway
    py = SimpleNamespace(language="python", install_cmd="uv sync", repo="a/b")
    assert pkgcheck.missing(py, co, "packages/workflow", run_cmd=run("MISSING\n")) is None                 # only picked installs


def test_a_chosen_package_joins_the_install_list_for_good(monkeypatch):
    monkeypatch.setattr(pkgcheck, "extras", lambda repo: ["@ai-sdk/harness"])
    p = profiles.get("vercel/ai")
    assert "--filter '@ai-sdk/harness...'" in p.filters and p.filters.count("@ai-sdk/workflow...") == 1
    assert "--filter '@ai-sdk/harness...'" in pkgcheck.recipe(p) and "FROM " in pkgcheck.recipe(p)
    with pytest.raises(ValueError):
        pkgcheck.add("vercel/ai", "x; rm -rf /")


@pytest.mark.parametrize("answer", ["yes", "no"])
def test_a_missing_package_pauses_the_run_and_asks(answer, scratch_db, tmp_path, monkeypatch):
    co = _checkout(tmp_path)
    asked, built, added = [], [], []
    monkeypatch.setattr(pkgcheck, "missing", lambda prof, checkout, pkg, run_cmd=None, trust_list=True:
                        None if built else {"dir": "packages/workflow", "name": "@ai-sdk/workflow"})
    monkeypatch.setattr(graph, "interrupt", lambda q: (asked.append(q), answer)[1])
    monkeypatch.setattr(pkgcheck, "rebuild", lambda prof, checkout, log=print: built.append(prof.filters))
    monkeypatch.setattr(pkgcheck, "add", lambda repo, name: added.append((repo, name)))
    ctx = SimpleNamespace(package_dir="packages/workflow", source="packages/workflow/src/model-call-iterator.ts")
    s = {"run_id": "ai-22085-z", "profile": {"repo": "vercel/ai"}}
    from debug_assist import events
    with events.bind("ai-22085-z", "reproduce"):
        got = graph._install_if_missing(s, co, ctx, {"rungs": ["unit"]}, [])
    assert asked == [{"kind": "install", "package": "@ai-sdk/workflow", "dir": "packages/workflow", "repo": "vercel/ai",
                      "minutes": pkgcheck.MINUTES}]
    if answer == "yes":
        assert got is None and len(built) == 1 and "'@ai-sdk/workflow...'" in built[0] and added == [("vercel/ai", "@ai-sdk/workflow")]
    else:
        assert got["outcome"]["exit"] == "TEST MACHINE NOT READY" and "you chose not to install it" in got["outcome"]["why"]
        assert not built and not added and got["repro"]["attempts_used"] == 0


def test_a_failed_rebuild_stops_the_run_and_says_why(scratch_db, tmp_path, monkeypatch):
    co = _checkout(tmp_path)
    monkeypatch.setattr(pkgcheck, "missing", lambda *a, **k: {"dir": "packages/workflow", "name": "@ai-sdk/workflow"})
    monkeypatch.setattr(graph, "interrupt", lambda q: "yes")
    def boom(prof, checkout, log=print):
        raise RuntimeError("E2B build failed: out of credit")
    monkeypatch.setattr(pkgcheck, "rebuild", boom)
    ctx = SimpleNamespace(package_dir="packages/workflow", source="x.ts")
    from debug_assist import events
    with events.bind("ai-22085-w", "reproduce"):
        got = graph._install_if_missing({"run_id": "ai-22085-w", "profile": {"repo": "vercel/ai"}}, co, ctx, {}, [])
    assert "installing @ai-sdk/workflow failed: E2B build failed: out of credit" in got["outcome"]["why"]


def test_the_page_asks_in_a_popup_and_a_saved_page_gives_the_commands():
    ask = {"kind": "install", "package": "@ai-sdk/workflow", "dir": "packages/workflow", "repo": "vercel/ai", "minutes": 3}
    st = {k: v for k, v in _data()["state"].items() if k not in ("repro", "attempts", "fix_clock", "second_story")}
    d = _data(state=st, interrupt=ask, next=["reproduce"], ask=ask)   # paused before any test was written
    page = viewer.render(d, mode="live", token="t")
    sheet = _sheet(page, "installask")
    assert "Install @ai-sdk/workflow?" in sheet and 'data-install="yes"' in sheet and 'data-install="no"' in sheet
    assert "about 3 minutes and a few cents of E2B credit" in sheet and "Needs your answer" in sheet
    assert "Waiting for you: @ai-sdk/workflow isn&#x27;t installed on the test machine. Install it?" in page
    assert "Waiting for your answer" in page and "Check PR" not in page and 'fetch("/api/install"' in page
    assert 'a.showPopover()' in page                                                      # it opens by itself
    saved = viewer.render(d)
    assert "data-install" not in saved and "uv run debug-assist answer ai-1-x yes" in saved


def test_the_answer_continues_the_run_as_the_terminal_would(tmp_path, monkeypatch):
    (tmp_path / "ai-22085-z").mkdir()
    monkeypatch.setattr(server, "CFG", SimpleNamespace(runs_dir=tmp_path))
    asking = SimpleNamespace(tasks=[SimpleNamespace(interrupts=[SimpleNamespace(value={"kind": "install", "package": "@ai-sdk/workflow"})])])
    monkeypatch.setattr(viewer, "_app", lambda: SimpleNamespace(get_state=lambda cfg: asking))
    calls = []
    class Proc:
        def __init__(self, argv, **kw):
            calls.append(argv)
        def poll(self):
            return 0
    monkeypatch.setattr(server.subprocess, "Popen", Proc)
    monkeypatch.setitem(server._child, "proc", None)
    monkeypatch.setattr("debug_assist.events.log", lambda *a, **k: None)
    with pytest.raises(server.Refused, match="yes or no"):
        server.install_answer("ai-22085-z", "maybe")
    assert "rebuilt" in server.install_answer("ai-22085-z", "yes", by="isha-gh")
    assert calls[-1][2:] == ["debug_assist", "answer", "ai-22085-z", "yes", "--no-view"]
    monkeypatch.setattr(viewer, "_app", lambda: SimpleNamespace(get_state=lambda cfg: SimpleNamespace(tasks=[])))
    with pytest.raises(server.Refused, match="not asking to install"):
        server.install_answer("ai-22085-z", "yes")
