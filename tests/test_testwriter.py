"""The test writer: where it looks, what it refuses, and one attempt end to end with a fake model and fake sandbox."""
import subprocess
from types import SimpleNamespace

import pytest

from debug_assist import ladder, testwriter
from debug_assist.testwriter import WriterRefused, anchors, locate, parse, validate

UNIT, INTEG = ladder.RUNGS[0], ladder.RUNGS[1]


def test_anchors_keep_exact_strings_and_drop_ids_and_versions():
    text = 'msg `Response stream ended without a finish reason.` id `gen_01M38TX` v `7.0.111` and "AI_InvalidResponseDataError"'
    assert anchors(text) == ["Response stream ended without a finish reason.", "AI_InvalidResponseDataError"]


@pytest.fixture
def repo(tmp_path):
    """A tiny monorepo shaped like vercel/ai: the focus names one package; another package matches the issue's
    other problem more often."""
    def put(rel, text):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    put("packages/gateway/src/errors.ts", "Error.message\ngetErrorMessage(error)\n{ name }\nAPICallError\n")
    put("packages/acme-compatible/src/chat/model.ts",
        "\n" * 30 + "flush(controller) {\n  tracker.flush();\n  message: 'Stream ended without a finish reason.',\n}\n")
    put("packages/acme-compatible/src/chat/model.test.ts",
        "import fs from 'fs';\nconst server = createTestServer({});\n\n"
        "describe('doStream', () => {\n  it('streams', async () => {\n    response = { type: 'stream-chunks' };\n  });\n});\n")
    put("packages/acme-compatible/src/chat/__fixtures__/acme-tool-call.chunks.txt", '{"choices":[]}\n')
    put("packages/acme-compatible/src/chat/__fixtures__/acme-text.chunks.txt", '{"choices":[]}\n')
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def test_locate_follows_the_focus_not_the_louder_other_problem(repo):
    issue = ("Gateway drops `Error.message` via `getErrorMessage(error)`, `{ name }`, `APICallError`. "
             "Also `Stream ended without a finish reason.`")
    focus = "The acme-compatible stream flush turns a half-streamed tool call into a complete one."
    ctx = locate(repo, issue, focus)
    assert ctx.source == "packages/acme-compatible/src/chat/model.ts" and ctx.package == "acme-compatible"
    assert ctx.example_test == "packages/acme-compatible/src/chat/model.test.ts"
    assert "tracker.flush()" in ctx.snippets and "createTestServer" in ctx.example_header
    assert "stream-chunks" in ctx.example_case
    assert ctx.fixture_best.startswith("packages/acme-compatible/src/chat/__fixtures__/acme-tool-call")


def test_parse_takes_the_ts_block_and_the_symptom():
    content, symptom = parse("here\n```ts\nit('x', () => expect(1).toBe(2));\n```\nSYMPTOM: emits tool-call")
    assert content.startswith("it('x'") and symptom == "emits tool-call"
    with pytest.raises(WriterRefused, match="no ```ts block"):
        parse("just prose")


@pytest.mark.parametrize("content,rung,why", [
    ("it('x', () => {})", UNIT, "asserts nothing"),
    ("it.only('x', () => expect(1))", UNIT, ".only"),
    ("const k = process.env.KEY; expect(k)", UNIT, "env vars"),
    ("await fetch('https://api.openai.com/v1'); expect(1)", UNIT, "real host"),
    ("expect(1); // no fixture", INTEG, "recorded fixture"),
    ("fs.readFileSync('src/chat/__fixtures__/x.chunks.txt'); expect(1)", UNIT, "made-up input"),
])
def test_validate_refuses_what_the_rules_forbid(content, rung, why):
    with pytest.raises(WriterRefused, match=why):
        validate(content, rung)


def test_validate_accepts_a_fixture_based_integration_test():
    validate("const l = fs.readFileSync('src/chat/__fixtures__/x.chunks.txt','utf8'); expect(l).toBe(1)", INTEG)
    validate("server.urls['https://my.api.com/v1/chat/completions']; expect(1)", UNIT)  # the mock host is fine


def _ctx(repo):
    return locate(repo, "`Stream ended without a finish reason.`", "acme-compatible stream flush")


def test_one_attempt_writes_a_new_file_runs_it_offline_and_classifies(repo, monkeypatch):
    reply = "```ts\nimport fs from 'fs';\nit('withholds', () => expect([1]).toStrictEqual([]));\n```\nSYMPTOM: a tool-call"
    monkeypatch.setattr(testwriter, "write", lambda state, step, msgs, max_tokens: (SimpleNamespace(content=reply), {}))
    ran = {}

    def fake_run(cmd, workdir, network, timeout, image):
        ran.update(cmd=cmd, network=network)
        return SimpleNamespace(returncode=1, stdout="AssertionError: expected [ 1 ] to strictly equal []\n- Expected\n+ Received\n 1 failed", stderr="")
    profile = SimpleNamespace(env="ENV && ", test_cmd="cd packages/{package} && pnpm test:node {test_path}",
                              image="node", language="typescript")
    state = {"issue": {"number": 7, "title": "t", "body": "b"}, "run_id": "r"}
    a = testwriter.attempt(state, UNIT, 1, [], _ctx(repo), repo, profile, run_cmd=fake_run, drafts=repo / "new" / "drafts")
    assert (repo / "new" / "drafts" / "1-unit.md").exists()  # the drafts folder is created on demand
    assert a.outcome == ladder.RED and a.test_path == "packages/acme-compatible/src/chat/da-repro-7-unit-1.test.ts"
    assert (repo / a.test_path).exists() and ran["network"] is False
    assert ran["cmd"] == "ENV && cd packages/acme-compatible && pnpm test:node src/chat/da-repro-7-unit-1.test.ts"
    assert "AssertionError" in a.evidence and "a tool-call" in a.evidence
    assert subprocess.run(["git", "diff", "--quiet"], cwd=repo).returncode == 0, "no existing file may change"


def test_a_refused_draft_is_an_error_attempt_and_writes_nothing(repo, monkeypatch):
    monkeypatch.setattr(testwriter, "write", lambda *a, **k: (SimpleNamespace(content="```ts\nexpect(1)\n```"), {}))
    state = {"issue": {"number": 7, "title": "t", "body": "b"}, "run_id": "r"}
    a = testwriter.attempt(state, INTEG, 2, [], _ctx(repo), repo, SimpleNamespace(), run_cmd=None)
    assert a.outcome == ladder.ERROR and "recorded fixture" in a.evidence and a.test_path == ""
    assert not list(repo.rglob("da-repro-*"))


def test_parse_strips_a_symptom_line_written_inside_the_code_and_accepts_a_cut_off_block():
    content, symptom = parse("SYMPTOM: emits tool-call\n```ts\nexpect(1);\nSYMPTOM: emits tool-call\n")
    assert "SYMPTOM" not in content and content.startswith("expect(1)") and symptom == "emits tool-call"


FOCUS = 'The stream flush turns a half-streamed tool call into a complete-looking `tool-call` part (here `input: ""`).'


def test_a_red_with_the_focus_symptom_counts():
    out = 'AssertionError: expected [ { type: \'tool-call\' } ] to strictly equal []\n+   "input": "",\n'
    assert testwriter.right_reason(FOCUS, out) is True


def test_a_red_for_another_reason_does_not_count():
    """Trial 2026-10-07: a test of the issue's other problem failed on a wrong expectation."""
    out = ("AssertionError: expected 'AI_InvalidResponseDataError' to be 'InvalidResponseDataError'\n"
           "Expected: \"InvalidResponseDataError\"\nReceived: \"AI_InvalidResponseDataError\"\n")
    assert testwriter.symptom_terms(FOCUS) == ["tool-call", "input"]
    assert testwriter.symptom_terms("Before emitting the `error`, `tool-call`") == ["tool-call"]
    assert testwriter.right_reason(FOCUS, out) is False
    assert testwriter.right_reason("no code strings here", out) is None


def test_a_wrong_reason_red_becomes_an_error_attempt(repo, monkeypatch):
    reply = "SYMPTOM: name differs\n```ts\nit('x', () => expect('a').toBe('b'));\n```"
    monkeypatch.setattr(testwriter, "write", lambda *a, **k: (SimpleNamespace(content=reply), {}))
    fake = lambda *a, **k: SimpleNamespace(returncode=1, stdout="AssertionError: expected 'a' to be 'b'\n 1 failed", stderr="")
    profile = SimpleNamespace(env="", test_cmd="cd packages/{package} && pnpm test:node {test_path}", image="n",
                              language="typescript")
    state = {"issue": {"number": 7, "title": "t", "body": "b"}, "run_id": "r", "focus": FOCUS}
    a = testwriter.attempt(state, UNIT, 1, [], _ctx(repo), repo, profile, run_cmd=fake)
    assert a.outcome == ladder.ERROR and "another reason" in a.evidence


def test_the_symptom_must_be_in_the_assertion_not_in_the_source_printed_beside_it():
    """Trial 2026-10-07, attempt 2: 'tool-call' sat only in a code comment that vitest printed under the failure."""
    out = ("AssertionError: expected [] to have a length of 1 but got +0\n- Expected\n+ Received\n- 1\n+ 0\n"
           " ❯ src/chat/x.test.ts:51:25\n     50|     // There should be ZERO tool-call events\n"
           "     51|     expect(errorEvents).toHaveLength(1);\n")
    assert testwriter.right_reason(FOCUS, out) is False
    real = ("AssertionError: expected [ { type: 'tool-call', …(3) } ] to have a length of +0 but got 1\n"
            "- Expected\n+ Received\n- 0\n+ 1\n ❯ src/chat/x.test.ts:55:27\n")
    assert testwriter.right_reason(FOCUS, real) is True


def test_the_word_error_in_assertion_error_proves_nothing():
    """Trial 2026-10-07: a focus containing `error` matched every failure through the label AssertionError."""
    focus = "Before emitting the `error`, the flush emits a complete-looking `tool-call` part"
    wrong = "AssertionError: expected AI_JSONParseError: JSON parsing failed to be an instance of X\n"
    count = "AssertionError: expected 1 to be +0 // Object.is equality\n- Expected\n+ Received\n\n- 0\n+ 1\n"
    assert testwriter.right_reason(focus, wrong) is False and testwriter.right_reason(focus, count) is False
