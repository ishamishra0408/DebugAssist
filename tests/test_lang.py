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
    assert j == {"passed": ["test_guard[empty]"], "symptom": ["test_guard[no-args]"], "broken": []}


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
        {"passed": ["clean control"], "symptom": ["gzip blob"], "broken": []}
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
