"""The bug pipeline on a Python repo: the same steps as vercel/ai, with the layout, test framework and output reading
from the Python adapter (lang.py). A small fake uv monorepo, a fake sandbox, no model calls."""
import subprocess
from types import SimpleNamespace

import pytest

from debug_assist import context, fixer, guard, lang, ladder, testwriter
from debug_assist.profiles import RepoProfile

PROFILE = RepoProfile("acme/py", "python", "python:3.12-slim", install_cmd="", source="connected", manager="uv",
                      test_cmd="cd {package_dir} && uv run --no-sync pytest -q {test_path}", env="export CI=1 && ",
                      package_globs=("libs/core", "libs/partners/openai"), runner="pytest",
                      source_globs=("libs/core/**", "libs/partners/openai/**"), test_style="tests/unit_tests",
                      test_suffix="test_*.py")

FILES = {
    "libs/core/pyproject.toml": '[project]\nname = "acme-core"\ndependencies = ["pydantic>=2"]\n',
    "libs/core/acme_core/__init__.py": "",
    "libs/core/acme_core/messages.py": (
        "from pydantic import BaseModel\n\n\nclass ToolCall(BaseModel):\n    name: str\n    args: dict\n\n\n"
        "def merge_chunks(chunks):\n    out = {}\n    for c in chunks:\n        out.update(c)\n"
        "    return ToolCall(name=out.get('name', ''), args={})  # drops `args` from streamed chunks\n"),
    "libs/core/tests/unit_tests/conftest.py": "import pytest\n",
    "libs/core/tests/unit_tests/test_messages.py": (
        "from acme_core.messages import merge_chunks\n\n\n@pytest.mark.parametrize('n', [1])\n"
        "def test_merge_keeps_name(n):\n    assert merge_chunks([{'name': 'x'}]).name == 'x'\n\n\n"
        "def test_merge_empty():\n    assert merge_chunks([]).name == ''\n"),
    "libs/core/tests/integration_tests/test_live.py": "def test_live():\n    assert True\n",
    "libs/partners/openai/pyproject.toml": '[project]\nname = "acme-openai"\ndependencies = ["acme-core>=0.1"]\n',
    "libs/partners/openai/acme_openai/chat.py": (
        "from acme_core.messages import ToolCall, merge_chunks\n\n\n"
        "def stream(chunks):\n    call = merge_chunks(chunks)\n    return [call]\n"),
    "libs/partners/openai/tests/unit_tests/test_chat.py": "def test_stream():\n    assert True\n",
}


@pytest.fixture
def repo(tmp_path):
    for rel, text in FILES.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    for c in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b"]):
        subprocess.run(c, cwd=tmp_path, check=True)
    return tmp_path


def test_the_adapter_follows_the_profile():
    py, js = lang.of(PROFILE), lang.of(SimpleNamespace(language="typescript"))
    assert (py.key, py.framework, py.fence, js.key, js.framework) == ("python", "pytest", "python", "js", "vitest")
    assert py.package_of("libs/partners/openai/acme_openai/chat.py") == "libs/partners/openai"
    assert py.package_of("libs/core/acme_core/messages.py") == "libs/core"
    assert js.package_of("packages/ai/src/x.ts") == "packages/ai"                 # vercel/ai's defaults, unchanged
    assert py.is_source("libs/core/acme_core/messages.py") and not py.is_source("libs/core/tests/unit_tests/test_x.py")
    assert not py.is_source("libs/core/acme_core/latest_test.py") and py.is_source("libs/core/acme_core/latest_x.py")
    assert py.test_command("libs/core", "libs/core/tests/unit_tests/test_a.py", py.VERBOSE) == \
        "export CI=1 && cd libs/core && uv run --no-sync pytest -q tests/unit_tests/test_a.py -vv -rA"
    assert py.test_command("libs/core") == "export CI=1 && cd libs/core && uv run --no-sync pytest -q"
    assert py.new_test("libs/core/tests/unit_tests/test_messages.py", "repro", 7, "unit", 1) == \
        "libs/core/tests/unit_tests/test_da_repro_7_unit_1.py"
    assert py.rebuild_command(["acme-core"]) == ""                                  # editable installs: nothing to build


def test_locate_finds_the_source_and_the_test_beside_it(repo):
    ctx = testwriter.locate(repo, "Streaming drops `args` from the tool call", "merge_chunks drops `args`", PROFILE)
    assert ctx.source == "libs/core/acme_core/messages.py" and ctx.package_dir == "libs/core" and ctx.package == "core"
    assert ctx.example_test == "libs/core/tests/unit_tests/test_messages.py"      # unit tests, never integration
    assert ctx.example_header.startswith("from acme_core.messages import merge_chunks")
    assert ctx.example_case.startswith("@pytest.mark.parametrize") and "def test_merge_keeps_name" in ctx.example_case


def test_one_python_attempt_writes_a_new_test_file_and_reads_pytest(repo, monkeypatch):
    ctx = testwriter.locate(repo, "drops `args`", "merge_chunks drops `args`", PROFILE)
    reply = ("SYMPTOM: args is empty\n```python\nfrom acme_core.messages import merge_chunks\n\n\n"
             "def test_args_kept():\n    assert merge_chunks([{'name': 'x', 'args': {'a': 1}}]).args == {'a': 1}\n```")
    monkeypatch.setattr(testwriter, "write", lambda *a, **k: (SimpleNamespace(content=reply), None))
    ran = []
    out = ("F\n=================================== FAILURES ===================================\n"
           "_______________________________ test_args_kept ________________________________\n"
           "E       AssertionError: assert {} == {'a': 1}\nE         Right contains 1 more item: {'a': 1}\n"
           "FAILED tests/unit_tests/test_da_repro_9_unit_1.py::test_args_kept - AssertionError: assert {} == {'a': 1}\n"
           "1 failed in 0.05s\n")
    run = lambda cmd, wd, **k: (ran.append(cmd), subprocess.CompletedProcess(cmd, 1, out, ""))[1]  # noqa: E731
    state = {"issue": {"number": 9, "title": "drops args", "body": "drops `args`"}, "focus": "merge_chunks drops `args`"}
    a = testwriter.attempt(state, ladder.RUNGS[0], 1, [], ctx, repo, PROFILE, run_cmd=run)
    assert a.outcome == ladder.RED and a.test_path == "libs/core/tests/unit_tests/test_da_repro_9_unit_1.py"
    assert (repo / a.test_path).read_text().startswith("from acme_core.messages import merge_chunks")
    assert ran == ["export CI=1 && cd libs/core && uv run --no-sync pytest -q tests/unit_tests/test_da_repro_9_unit_1.py"]


@pytest.mark.parametrize("content,why", [
    ("def test_x():\n    pass\n", "asserts nothing"),
    ("import pytest\n\n@pytest.mark.skip\ndef test_x():\n    assert 1\n", "skipped"),
    ("import httpx\n\ndef test_x():\n    httpx.get('https://api.openai.com/v1')\n    assert 1\n", "real host"),
    ("assert 1\n", "no test function"),
])
def test_a_python_draft_is_checked_in_code(content, why):
    with pytest.raises(testwriter.WriterRefused, match=why):
        testwriter.validate(content, ladder.RUNGS[0], lang.of(PROFILE))
    testwriter.validate("def test_x(monkeypatch):\n    monkeypatch.setenv('OPENAI_API_KEY', 'fake')\n    assert 1 == 1\n",
                        ladder.RUNGS[0], lang.of(PROFILE))


def test_a_python_fix_edits_source_only_and_finds_definitions(repo):
    with pytest.raises(fixer.FixRefused, match="tests or fixtures"):
        fixer.apply_edits(repo, [fixer.Edit("libs/core/tests/unit_tests/test_messages.py", "x", "y")], PROFILE)
    with pytest.raises(fixer.FixRefused, match="source only"):
        fixer.apply_edits(repo, [fixer.Edit("scripts/release.py", "x", "y")], PROFILE)
    assert fixer.apply_edits(repo, [fixer.Edit("libs/core/acme_core/messages.py", "args={}", "args=out.get('args', {})")],
                             PROFILE) == ["libs/core/acme_core/messages.py"]
    found = fixer.find_definition(repo, "merge_chunks", profile=PROFILE)
    assert found.startswith("--- libs/core/acme_core/messages.py (definition of merge_chunks at line 9)")
    assert "class ToolCall" in fixer.find_definition(repo, "ToolCall", profile=PROFILE)


def test_a_change_to_a_python_package_runs_its_dependents(repo):
    pmap = fixer.package_map(repo, PROFILE)
    assert pmap["acme-openai"] == {"dir": "libs/partners/openai", "deps": {"acme-core"}}
    names = fixer.suite_names(PROFILE, repo)
    assert names == ["acme-core", "acme-openai"]                                  # a connected repo installs them all
    changed, suites = fixer.affected(repo, ["libs/core/acme_core/messages.py"], names, PROFILE)
    assert changed == ["acme-core"] and suites == ["libs/core", "libs/partners/openai"]


def test_pytest_cases_and_failure_blocks():
    out = ("tests/unit_tests/test_da_guard_9.py::test_guard[no-args] FAILED                  [ 50%]\n"
           "tests/unit_tests/test_da_guard_9.py::test_guard[empty] PASSED                    [100%]\n"
           "=================================== FAILURES ===================================\n"
           "____________________________ test_guard[no-args] ______________________________\n"
           "E       AssertionError: assert {} == {'a': 1}\n"
           "E        +  where {} = ToolCall(name='x', args={}).args\n"
           "=========================== short test summary info ============================\n"
           "PASSED tests/unit_tests/test_da_guard_9.py::test_guard[empty]\n"
           "FAILED tests/unit_tests/test_da_guard_9.py::test_guard[no-args] - AssertionError\n")
    py = lang.of(PROFILE)
    assert guard.cases(out, py) == {"passed": ["test_guard[empty]"], "failed": ["test_guard[no-args]"]}
    assert "assert {} == {'a': 1}" in guard.failure_blocks(out, py)["test_guard[no-args]"]
    j = guard.judged("merge_chunks drops `args`", out, py)
    assert j == {"passed": ["test_guard[empty]"], "symptom": ["test_guard[no-args]"], "broken": [], "checked": True}


def test_shared_code_is_found_through_python_imports(repo):
    ctx = testwriter.locate(repo, "`stream` drops `args`", "acme-openai stream drops `args` from merge_chunks", PROFILE)
    assert ctx.source == "libs/partners/openai/acme_openai/chat.py"
    rel = context.related(repo, ctx, PROFILE)
    assert [r["name"] for r in rel][:1] == ["merge_chunks"] and rel[0]["definition"].startswith("--- libs/core/")


# ── plain Node (NoLeakMCP's shape): test files run with `node <file>`, node:test's TAP output ──────────────────
NODE = RepoProfile("i/nl", "javascript", "node:20-slim", install_cmd="true", source="connected", manager="npm",
                   runner="node", env="export CI=1 && ", package_globs=(".",),
                   source_globs=("plugins/**", "render/**"), test_style="tests", test_suffix=".test.mjs",
                   test_cmd='cd {package_dir} || exit 1; rc=0; for f in {test_path}; do node "$f" || rc=1; done; exit $rc',
                   test_glob="tests/guard.test.mjs")

NODE_FILES = {
    "plugins/mcp-guard/package.json": '{"name": "dsh-mcp-guard"}',
    "plugins/mcp-guard/index.js": ("export function scanArguments(args, opts) {\n  // decode `gzip` blobs before matching\n"
                                   "  return { hit: 'clean' };\n}\n"),
    "render/arena/arena-core.mjs": "import { scanArguments } from '../../plugins/mcp-guard/index.js';\nexport const run = () => scanArguments({});\n",
    "tests/guard.test.mjs": ('import { test } from "node:test";\nimport assert from "node:assert/strict";\n'
                             'import { scanArguments } from "../plugins/mcp-guard/index.js";\n\n'
                             'test("raw slack message", () => {\n  assert.equal(scanArguments({}).hit, "clean");\n});\n'),
    "tests/arena.test.mjs": 'import { test } from "node:test";\nimport { run } from "../render/arena/arena-core.mjs";\n',
}

TAP = """TAP version 13
# Subtest: gzip blob
not ok 1 - gzip blob
  ---
  duration_ms: 1.1
  failureType: 'testCodeFailure'
  error: |-
    Expected values to be strictly deep-equal:
    + actual - expected

      {
    +   hit: 'clean'
    -   hit: 'gzip'
      }
  code: 'ERR_ASSERTION'
  name: 'AssertionError'
  ...
# Subtest: clean control
ok 2 - clean control
  ---
  duration_ms: 0.2
  ...
1..2
# fail 1
"""


@pytest.fixture
def node_repo(tmp_path):
    for rel, text in NODE_FILES.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    for c in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b"]):
        subprocess.run(c, cwd=tmp_path, check=True)
    return tmp_path


def test_plain_node_locates_the_source_and_the_test_that_imports_it(node_repo):
    ctx = testwriter.locate(node_repo, "scanArguments misses a `gzip` blob", "`scanArguments` misses a `gzip` blob", NODE)
    assert ctx.source == "plugins/mcp-guard/index.js" and ctx.package_dir == "."
    assert ctx.example_test == "tests/guard.test.mjs"                               # it imports the very file
    assert 'import { test } from "node:test";' in ctx.example_header and ctx.example_case.startswith('test("raw slack')


def test_a_plain_node_attempt_writes_a_node_test_beside_the_example_and_reads_tap(node_repo, monkeypatch):
    ctx = testwriter.locate(node_repo, "misses a `gzip` blob", "`scanArguments` returns `clean` for a `gzip` blob", NODE)
    reply = ('SYMPTOM: hit is clean\n```js\nimport { test } from "node:test";\nimport assert from "node:assert/strict";\n'
             'import { scanArguments } from "../plugins/mcp-guard/index.js";\n'
             'test("gzip blob", () => { assert.deepStrictEqual(scanArguments({}), { hit: "gzip" }); });\n```')
    monkeypatch.setattr(testwriter, "write", lambda *a, **k: (SimpleNamespace(content=reply), None))
    ran = []
    run = lambda cmd, wd, **k: (ran.append(cmd), subprocess.CompletedProcess(cmd, 1, TAP, ""))[1]  # noqa: E731
    state = {"issue": {"number": 3, "title": "gzip", "body": ""}, "focus": "`scanArguments` returns `clean` for a `gzip` blob"}
    a = testwriter.attempt(state, ladder.RUNGS[0], 1, [], ctx, node_repo, NODE, run_cmd=run)
    assert a.outcome == ladder.RED and a.test_path == "tests/da-repro-3-unit-1.test.mjs"   # failed for the focus's reason
    assert ran == ['export CI=1 && cd . || exit 1; rc=0; for f in tests/da-repro-3-unit-1.test.mjs; do node "$f" || rc=1; done; exit $rc']


@pytest.mark.parametrize("content,why", [
    ('import { test } from "node:test";\ntest("x", () => {});\n', "no node:assert"),
    ('import assert from "node:assert/strict";\nassert.equal(1, 2);\n', "use node:test"),
    ('import { test } from "node:test";\nimport assert from "node:assert";\ntest.only("x", () => assert.ok(1));\n', ".only"),
    ('import { test } from "node:test";\nimport assert from "node:assert";\ntest("x", async () => { await fetch("https://api.linkup.so/v1"); assert.ok(1) });\n', "real host"),
])
def test_a_plain_node_draft_is_checked_in_code(content, why):
    with pytest.raises(testwriter.WriterRefused, match=why):
        testwriter.validate(content, ladder.RUNGS[0], lang.of(NODE))
    testwriter.validate('import { test } from "node:test";\nimport assert from "node:assert/strict";\n'
                        'process.env.NEBIUS_API_KEY = "fake";\ntest("x", async () => { await fetch("http://127.0.0.1:1/"); assert.ok(1) });\n',
                        ladder.RUNGS[0], lang.of(NODE))


def test_node_test_tap_is_read_case_by_case():
    nl = lang.of(NODE)
    assert guard.cases(TAP, nl) == {"passed": ["clean control"], "failed": ["gzip blob"]}
    assert "Expected values to be strictly deep-equal" in guard.failure_blocks(TAP, nl)["gzip blob"]
    assert guard.judged("`scanArguments` returns `clean` for a `gzip` blob", TAP, nl) == \
        {"passed": ["clean control"], "symptom": ["gzip blob"], "broken": [], "checked": True}
    assert any("+   hit: 'clean'" in t for t in testwriter.assertion_text(TAP))       # the actual side, not expected
    assert not any("hit: 'gzip'" in t for t in testwriter.assertion_text(TAP))


def test_a_plain_node_fix_runs_the_files_that_passed_at_connection(node_repo):
    pmap = fixer.package_map(node_repo, NODE)
    assert pmap["(repo)"] == {"dir": ".", "deps": set()}                              # no manifest at the top
    names = fixer.suite_names(NODE, node_repo)
    changed, suites = fixer.affected(node_repo, ["plugins/mcp-guard/index.js"], names, NODE)
    assert changed == ["(repo)"] and suites == ["."]
    assert lang.of(NODE).test_command(".").endswith('for f in tests/guard.test.mjs; do node "$f" || rc=1; done; exit $rc')
    with pytest.raises(fixer.FixRefused, match="tests or fixtures"):
        fixer.apply_edits(node_repo, [fixer.Edit("tests/guard.test.mjs", "x", "y")], NODE)
    with pytest.raises(fixer.FixRefused, match="source only"):
        fixer.apply_edits(node_repo, [fixer.Edit("README.md", "x", "y")], NODE)
    assert "export function scanArguments" in fixer.find_definition(node_repo, "scanArguments", profile=NODE)


def test_your_pointers_lead_the_search_and_the_unit_test_goes_into_your_file(repo, monkeypatch, tmp_path_factory):
    """Isha 2026-10-08: optional pointers at the start: files to look in for the cause, and the test file the unit test
    is written in (new cases at its end; nothing else in it changes; put back when they do not show the bug)."""
    mine = "libs/core/tests/unit_tests/test_messages.py"
    before = (repo / mine).read_text()
    ctx = testwriter.locate(repo, "drops `args`", "merge_chunks drops `args`", PROFILE,
                            {"look_in": ["libs/partners/openai/acme_openai/chat.py", "libs/core/nope.py", mine],
                             "test_in": mine})
    assert ctx.source == "libs/partners/openai/acme_openai/chat.py"                 # pointed to: searched first
    assert ctx.look_in == ["libs/partners/openai/acme_openai/chat.py"]
    assert ctx.look_in_missing == ["libs/core/nope.py", mine]                       # not found / not a source file
    assert ctx.test_into == mine and ctx.example_test == mine and ctx.example_header.startswith("from acme_core.messages")
    asked, ran = [], []
    reply = "SYMPTOM: args is empty\n```python\ndef test_args_kept():\n    assert merge_chunks([{'args': {'a': 1}}]).args == {'a': 1}\n```"
    monkeypatch.setattr(testwriter, "write", lambda s, step, msgs, **k: (asked.append(msgs), (SimpleNamespace(content=reply), None))[1])
    red = ("E       AssertionError: assert {} == {'a': 1}\n"
           "FAILED tests/unit_tests/test_messages.py::test_args_kept - AssertionError: assert {} == {'a': 1}\n1 failed, 2 passed\n")
    result = {"code": 0, "out": "3 passed in 0.01s\n"}
    run = lambda cmd, wd, **k: (ran.append(cmd), subprocess.CompletedProcess(cmd, result["code"], result["out"], ""))[1]  # noqa: E731
    state = {"issue": {"number": 9, "title": "drops args", "body": "drops `args`"}, "focus": "merge_chunks drops `args`"}
    proof = tmp_path_factory.mktemp("proof")
    green = testwriter.attempt(state, ladder.RUNGS[0], 1, [], ctx, repo, PROFILE, run_cmd=run, proof_dir=proof)
    assert asked[0][0][1].startswith("You add NEW pytest test cases to the END of an existing test file (" + mine)
    assert "SETUP OF THE FILE YOU ADD TO" in asked[0][1][1]
    assert green.outcome == ladder.GREEN and green.test_path == mine and (repo / mine).read_text() == before  # put back
    assert ran[0] == "export CI=1 && cd libs/core && uv run --no-sync pytest -q tests/unit_tests/test_messages.py"
    result.update(code=1, out=red)
    a = testwriter.attempt(state, ladder.RUNGS[0], 2, [], ctx, repo, PROFILE, run_cmd=run, proof_dir=proof)
    assert a.outcome == ladder.RED and a.test_path == mine
    assert (repo / mine).read_text() == before.rstrip("\n") + "\n\n" + reply.split("```python\n")[1].split("```")[0]
    got = testwriter.read_proof(proof / "try-2-test_messages.py.txt")              # each try keeps its own proof
    assert got["diff"].startswith(f"diff --git a/{mine} b/{mine}") and "new file mode" not in got["diff"]
    assert "+def test_args_kept():" in got["diff"] and "\n def test_merge_empty():" in got["diff"]
    # a try that is not the judge: its cases are kept with the run, the person's file is put back
    from debug_assist import graph
    rdir = tmp_path_factory.mktemp("run")
    assert graph.shelve_drafts(repo, rdir, [mine], keep=None) == [mine]
    assert (repo / mine).read_text() == before and "def test_args_kept" in (rdir / "attempt-tests/test_messages.py").read_text()



def test_every_listed_file_keeps_the_lines_around_the_issues_strings(repo):
    """Isha 2026-10-09: in Context info only the best match could be opened; the others had no lines saved."""
    ctx = testwriter.locate(repo, "merge_chunks drops `args`", "`merge_chunks` drops `args`", PROFILE)
    got = context.ranked(repo, ctx)
    assert got and all("snippets" in f for f in got)
    chat = next((f for f in got if f["path"].endswith("acme_openai/chat.py")), None)
    if chat:   # it uses merge_chunks: its numbered lines are kept
        assert "merge_chunks" in chat["snippets"] and chat["snippets"].lstrip().split()[0].isdigit()


def test_a_reproduction_the_issue_cannot_check_says_so(repo, monkeypatch):
    """Review of run #22085: the issue quoted no code, so the failure could only be judged by its assertion; the run
    now says that instead of implying the symptom was matched."""
    ctx = testwriter.locate(repo, "drops `args`", "merge_chunks drops `args`", PROFILE)
    reply = "SYMPTOM: args is empty\n```python\ndef test_args_kept():\n    assert merge_chunks([{'args': {'a': 1}}]).args == {'a': 1}\n```"
    monkeypatch.setattr(testwriter, "write", lambda *a, **k: (SimpleNamespace(content=reply), None))
    out = "E       AssertionError: assert {} == {'a': 1}\nFAILED tests/unit_tests/test_x.py::test_args_kept - AssertionError\n1 failed\n"
    run = lambda cmd, wd, **k: subprocess.CompletedProcess(cmd, 1, out, "")  # noqa: E731
    state = {"issue": {"number": 9, "title": "merge drops the arguments"}, "focus": "merge drops the arguments"}
    a = testwriter.attempt(state, ladder.RUNGS[0], 1, [], ctx, repo, PROFILE, run_cmd=run)
    assert a.outcome == ladder.RED and testwriter.NOT_CHECKED in a.evidence
    state["focus"] = "merge_chunks drops `args`"                                  # quotes code: checked, no note
    b = testwriter.attempt(state, ladder.RUNGS[0], 2, [], ctx, repo, PROFILE, run_cmd=run)
    assert b.outcome == ladder.RED and testwriter.NOT_CHECKED not in b.evidence


def test_context_shows_what_really_ranked_each_file_and_flags_ties(repo):
    """Review of run #22085 (2026-10-09), F36: the reasons shown were the first strings present, not what ranked a
    file; version numbers were taken as code; a tie was broken by the file name, invisibly."""
    for junk in ("`7.0.128,`", "`5.0.1,`", "`4.5.4. The same code is on`", "`v2.0.59`"):
        assert testwriter.anchors(junk) == [], junk
    assert testwriter.anchors("`merge_chunks`, `tool-call`") == ["merge_chunks", "tool-call"]
    ctx = testwriter.locate(repo, "`merge_chunks` drops `args`", "`merge_chunks` drops `args`", PROFILE)
    got = context.ranked(repo, ctx)
    best = got[0]
    assert best["reasons"] and best["reasons"] == sorted(best["reasons"], key=lambda r: -r[1])     # heaviest first
    assert any("merge_chunks" in r[0] for r in best["reasons"]) and all(r[1] > 0 for r in best["reasons"])
    assert all(isinstance(f["tied_with"], list) for f in got)
    if len(got) > 1 and got[0]["score"] == got[1]["score"]:
        assert got[1]["path"] in got[0]["tied_with"]



def test_a_new_test_keeps_the_examples_environment_part():
    """vercel/ai's vue and react run only *.ui.test.ts(x): a test named da-repro-…-1.test.tsx would never run there."""
    js = lang.of(SimpleNamespace(language="typescript"))
    assert js.new_test("packages/vue/src/use-object.ui.test.tsx", "repro", 22543, "unit", 1) == \
        "packages/vue/src/da-repro-22543-unit-1.ui.test.tsx"
    assert js.new_test("packages/ai/src/ui/chat.test.ts", "repro", 1, "unit", 1) == "packages/ai/src/ui/da-repro-1-unit-1.test.ts"
    assert js.new_test("packages/workflow/src/a.integration.test.ts", "guard", 2) == "packages/workflow/src/da-guard-2.test.ts"


def test_the_test_script_is_read_from_the_runs_own_copy(tmp_path):
    """Review of run #22543 (F10): the script was read from the saved copy, which can be days older than main."""
    import json
    from debug_assist import lang as langs
    from debug_assist.profiles import PROFILES
    (tmp_path / "packages/vue").mkdir(parents=True)
    (tmp_path / "packages/vue/package.json").write_text(json.dumps({"scripts": {"test": "vitest"}}))
    js = langs.of(PROFILES["vercel/ai"])
    assert js.test_script("packages/vue", tmp_path) == "test"
    assert "cd packages/vue && pnpm test src/x.ui.test.ts" in js.test_command("packages/vue", "packages/vue/src/x.ui.test.ts", where=tmp_path)
    (tmp_path / "packages/vue/package.json").write_text(json.dumps({"scripts": {"test": "x", "test:node": "y"}}))
    assert js.test_script("packages/vue", tmp_path) == "test:node"
