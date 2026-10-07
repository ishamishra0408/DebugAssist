"""find_cause and write_fix: what they refuse, how they revert, and which suites must stay green."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from debug_assist import fixer
from debug_assist.fixer import Edit, FixRefused, affected, apply_edits, parse_cause, parse_edits


@pytest.fixture
def repo(tmp_path):
    """utils is shared; compat depends on it; app depends on compat; other depends on nothing changed."""
    def put(rel, text):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    for name, deps in (("utils", {}), ("compat", {"@x/utils": "1"}), ("app", {"@x/compat": "1"}), ("other", {})):
        put(f"packages/{name}/package.json", json.dumps({"name": f"@x/{name}", "dependencies": deps}))
    put("packages/utils/src/tracker.ts", "flush() {\n  emit(call);\n}\n")
    put("packages/compat/src/model.ts", "tracker.flush();\n")
    put("packages/compat/src/model.test.ts", "expect(x)\n")
    put("packages/compat/src/da-repro-1-unit-1.test.ts", "expect(parts).toStrictEqual([])\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    return tmp_path


def test_parse_cause_needs_a_real_source_file_and_lines(repo):
    ok = parse_cause("CAUSE_FILE: packages/utils/src/tracker.ts\nCAUSE_LINES: 1-3\nWHY: emits every call.\nFIX_PLAN: gate it.", repo)
    assert ok["file"] == "packages/utils/src/tracker.ts" and ok["lines"] == [1, 3] and "gate" in ok["plan"]
    for bad, why in [("CAUSE_FILE: packages/compat/src/model.test.ts\nCAUSE_LINES: 1-1\nWHY: x", "not a test"),
                     ("CAUSE_FILE: packages/nope.ts\nCAUSE_LINES: 1-1\nWHY: x", "no such file"),
                     ("CAUSE_FILE: packages/utils/src/tracker.ts\nCAUSE_LINES: 2-90\nWHY: x", "outside"),
                     ("it's the tracker", "no CAUSE_FILE")]:
        with pytest.raises(FixRefused, match=why):
            parse_cause(bad, repo)


def test_find_cause_can_look_up_a_definition_first(repo, monkeypatch):
    replies = iter(["NEED_DEFINITION: flush", "CAUSE_FILE: packages/utils/src/tracker.ts\nCAUSE_LINES: 1-3\nWHY: x"])
    seen = []
    monkeypatch.setattr(fixer, "write", lambda state, step, msgs, max_tokens: (
        seen.append(msgs[-1][1]) or SimpleNamespace(content=next(replies)), {}))
    monkeypatch.setattr(fixer, "find_definition", lambda co, n, **k: f"--- definition of {n}")
    ctx = SimpleNamespace(snippets="1  tracker.flush();", source="packages/compat/src/model.ts")
    cause = fixer.find_cause({"issue": {"title": "t"}, "focus": "f"}, repo, ctx,
                             "packages/compat/src/da-repro-1-unit-1.test.ts", "AssertionError")
    assert cause["looked_up"] == ["flush"] and "definition of flush" in seen[1]


def test_edits_never_touch_tests_and_must_match_exactly_once(repo):
    assert parse_edits("FILE: a.ts\n<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE")[0].replace == "new"
    with pytest.raises(FixRefused, match="may not edit tests"):
        apply_edits(repo, [Edit("packages/compat/src/da-repro-1-unit-1.test.ts", "expect", "// expect")])
    with pytest.raises(FixRefused, match="exactly one"):
        apply_edits(repo, [Edit("packages/utils/src/tracker.ts", "nothing like this", "x")])


def test_edits_are_all_or_nothing(repo):
    with pytest.raises(FixRefused):
        apply_edits(repo, [Edit("packages/utils/src/tracker.ts", "emit(call);", "if (done) emit(call);"),
                           Edit("packages/compat/src/model.ts", "missing", "x")])
    assert "if (done)" not in (repo / "packages/utils/src/tracker.ts").read_text()


def test_a_change_to_a_shared_package_runs_every_dependent_suite(repo):
    changed, suites = affected(repo, ["packages/utils/src/tracker.ts"], ["@x/utils", "@x/compat", "@x/app", "@x/other"])
    assert changed == ["@x/utils"] and suites == ["app", "compat", "utils"]  # transitive; 'other' untouched
    assert affected(repo, ["packages/compat/src/model.ts"], ["@x/compat", "@x/app"])[1] == ["app", "compat"]


PROFILE = SimpleNamespace(env="", build_cmd="pnpm {filters} build", image="n", language="typescript",
                          test_cmd="cd packages/{package} && pnpm test:node {test_path}",
                          filters="--filter '@x/utils...' --filter '@x/compat...' --filter '@x/app...'")
CAUSE = {"file": "packages/utils/src/tracker.ts", "lines": [1, 3], "why": "emits every call", "plan": "gate"}
GOOD = "FILE: packages/utils/src/tracker.ts\n<<<<<<< SEARCH\n  emit(call);\n=======\n  if (done) emit(call);\n>>>>>>> REPLACE"
BAD = "FILE: packages/utils/src/tracker.ts\n<<<<<<< SEARCH\n  emit(call);\n=======\n  // emit(call);\n>>>>>>> REPLACE"


def _runner(repo, calls):
    """Green only when the tracker has the gated line; records what ran."""
    def run(cmd, workdir, network, timeout, image):
        calls.append(cmd)
        fixed = "if (done)" in (repo / "packages/utils/src/tracker.ts").read_text()
        if "build" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "da-repro" in cmd:
            return SimpleNamespace(returncode=0 if fixed else 1, stdout="" if fixed else "AssertionError: x\n 1 failed", stderr="")
        return SimpleNamespace(returncode=0 if fixed else 1, stdout="AssertionError: suite broke\n 1 failed", stderr="")
    return run


def test_a_failed_attempt_is_reverted_and_the_next_one_validated(repo, monkeypatch):
    replies = iter([BAD, GOOD])
    monkeypatch.setattr(fixer, "write", lambda *a, **k: (SimpleNamespace(content=next(replies)), {}))
    calls = []
    fix = fixer.write_fix({"issue": {"title": "t"}}, repo, CAUSE, "packages/compat/src/da-repro-1-unit-1.test.ts",
                          "AssertionError", PROFILE, run_cmd=_runner(repo, calls))
    assert fix["status"] == "VALIDATED" and len(fix["attempts"]) == 2 and not fix["attempts"][0]["ok"]
    assert "still RED" in fix["attempts"][0]["evidence"]
    assert "+  if (done) emit(call);" in fix["patch"] and fix["suites"] == {"app": "pass", "compat": "pass", "utils": "pass"}
    assert any("--filter '@x/utils' build" in c for c in calls)  # the changed shared package was rebuilt first


def test_no_validated_fix_stops_with_the_source_restored(repo, monkeypatch):
    monkeypatch.setattr(fixer, "write", lambda *a, **k: (SimpleNamespace(content=BAD), {}))
    fix = fixer.write_fix({"issue": {"title": "t"}}, repo, CAUSE, "packages/compat/src/da-repro-1-unit-1.test.ts",
                          "AssertionError", PROFILE, run_cmd=_runner(repo, []))
    assert fix["status"] == "NOT VALIDATED" and len(fix["attempts"]) == fixer.FIX_ATTEMPTS and fix["patch"] == ""
    assert subprocess.run(["git", "diff", "--quiet"], cwd=repo).returncode == 0, "source must be back to the base"


def test_the_file_may_be_named_bare_inside_a_fence_and_reused_for_a_second_block():
    reply = ("Here is the fix.\n```typescript\npackages/utils/src/tracker.ts\n<<<<<<< SEARCH\n  emit(call);\n=======\n"
             "  if (done) emit(call);\n>>>>>>> REPLACE\n```\n\n```ts\n<<<<<<< SEARCH\nflush() {\n=======\n"
             "flush({ done = true } = {}) {\n>>>>>>> REPLACE\n```")
    edits = parse_edits(reply)
    assert [e.path for e in edits] == ["packages/utils/src/tracker.ts"] * 2 and edits[1].replace.startswith("flush({")
    with pytest.raises(FixRefused, match="names no file"):
        parse_edits("<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE")


def test_the_fixer_may_call_the_test_flawed_but_never_edit_it(repo, monkeypatch):
    monkeypatch.setattr(fixer, "write", lambda *a, **k: (SimpleNamespace(
        content="TEST_FLAWED: its second chunk is invalid JSON, so a parse error replaces the expected error"), {}))
    fix = fixer.write_fix({"issue": {"title": "t"}}, repo, CAUSE, "packages/compat/src/da-repro-1-unit-1.test.ts",
                          "AssertionError", PROFILE, run_cmd=_runner(repo, []))
    assert fix["status"] == "TEST FLAWED" and "invalid JSON" in fix["why"] and len(fix["attempts"]) == 1
    assert subprocess.run(["git", "diff", "--quiet"], cwd=repo).returncode == 0


def test_a_method_definition_is_found(repo):
    (repo / "packages/compat/src/model.ts").write_text("class Model {\n  async doStream(options) {\n    return 1;\n  }\n}\n")
    (repo / "packages/utils/src/other.ts").write_text("const doStream = 1;\n")  # same name elsewhere
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    assert "compat/src/model.ts (definition of doStream at line 2" in fixer.find_definition(repo, "doStream", prefer="packages/compat")
    assert "definition of Model at line 1" in fixer.find_definition(repo, "Model")  # \\s never matched in git grep


def test_a_search_off_only_in_indentation_still_applies_once_and_is_reindented(repo):
    (repo / "packages/utils/src/tracker.ts").write_text("class T {\n  flush() {\n    emit(call);\n  }\n}\n")
    changed = apply_edits(repo, [Edit("packages/utils/src/tracker.ts", "flush() {\n  emit(call);\n}",
                                      "flush(done = true) {\n  if (done) emit(call);\n}")])
    assert changed and (repo / "packages/utils/src/tracker.ts").read_text() == \
        "class T {\n  flush(done = true) {\n    if (done) emit(call);\n  }\n}\n"


def test_an_indent_blind_match_in_two_places_is_still_refused(repo):
    (repo / "packages/utils/src/tracker.ts").write_text("a() {\n  emit(call);\n}\nb() {\n    emit(call);\n}\n")
    with pytest.raises(FixRefused, match="exactly one"):
        apply_edits(repo, [Edit("packages/utils/src/tracker.ts", "        emit(call);", "x();")])  # 0 exact, 2 loose
