"""Gather context: one step, no AI. What it collects, what it leaves out, and that later steps read only the saved pack."""
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from debug_assist import context, testwriter


def test_names_never_travel_but_package_scopes_do():
    assert context.clean("thanks @octocat and @some-one, see @ai-sdk/provider-utils, mail a@b.co") == \
        "thanks @someone and @someone, see @ai-sdk/provider-utils, mail a@b.co"


def test_errors_and_stack_lines_are_pulled_out_of_the_discussion():
    got = context.errors_in("It throws\n```\nAI_InvalidResponseDataError: Expected 'function.name' to be a string.\n"
                            "    at processDelta (packages/provider-utils/src/streaming-tool-call-tracker.ts:157:23)\n```",
                            "TypeError: x is undefined")
    assert got["errors"][0].startswith("AI_InvalidResponseDataError: Expected") and "TypeError: x is undefined" in got["errors"]
    assert got["frames"] == ["packages/provider-utils/src/streaming-tool-call-tracker.ts:157"]


def test_linked_items_come_from_the_timeline_once_each(monkeypatch):
    ev = {"event": "cross-referenced", "source": {"issue": {"number": 7, "title": "fix by @octocat", "state": "closed",
                                                            "pull_request": {"merged_at": "x"}, "repository": {"full_name": "o/r"}}}}
    monkeypatch.setattr(context.github_read, "api", lambda path: [ev, ev, {"event": "labeled"}])
    assert context.linked("o", "r", 1) == [{"repo": "o/r", "number": 7, "title": "fix by @someone", "state": "closed",
                                            "pull_request": True, "merged": True}]


def test_shared_code_is_found_through_the_instance_the_problem_lines_use():
    src = ("import { combineHeaders, StreamingToolCallTracker, type ParseResult } from '@ai-sdk/provider-utils';\n"
           "import { local } from './local';\n" + "\n" * 20 +
           "let tracker: StreamingToolCallTracker<X>;\n" + "\n" * 60 +
           "flush() {\n  tracker.processDelta(pending);\n}\n" + "\n" * 60 + "const h = combineHeaders(a, b);\n")
    near = {src.count("\n", 0, src.index("tracker.processDelta")) + 1}
    got = context.imported_near(src, near)
    assert got[0] == ("StreamingToolCallTracker", "@ai-sdk/provider-utils")       # via its instance, on the line itself
    assert ("local", "./local") not in got and all(n != "combineHeaders" for n, _ in got)  # same package / too far


def test_the_brief_is_cut_to_size_and_says_what_it_cut():
    pack = {"issue": {"comments": [{"text": "x" * 1400} for _ in range(5)] + [{"text": "```\nrepro\n```"}],
                      "linked": [], "errors": {"errors": [], "frames": []}},
            "related": [], "history": [], "code": {"ranking": []}}
    text, cut = context.brief(pack)
    assert len(text) < context.LIMITS["discussion"] + 200 and "[comment 6]" in text.split("\n")[1]  # code first
    assert cut and "comments left out of the brief" in cut[0]


def test_later_steps_read_only_the_context_that_was_fingerprinted(tmp_path):
    ctx = testwriter.Context(package="p", source="packages/p/src/a.ts", snippets="1 x", example_test="t",
                             example_header="h", example_case="c")
    import dataclasses
    pack = {"code": {"ctx": dataclasses.asdict(ctx)}, "brief": "ISSUE DISCUSSION: y"}
    sha = context.save(pack, tmp_path / "context.json")
    state = {"context": {"path": str(tmp_path / "context.json"), "sha256": sha}}
    got = context.load_ctx(state)
    assert got.source == "packages/p/src/a.ts" and got.extra == "ISSUE DISCUSSION: y"
    (tmp_path / "context.json").write_text(json.dumps({**pack, "brief": "edited"}))
    with pytest.raises(ValueError, match="changed after it was saved"):
        context.load_ctx(state)
    assert context.load_ctx({}) is None  # runs from before the step: the caller locates as before


def test_gather_context_stops_plainly_when_no_code_matches(tmp_path, monkeypatch):
    from debug_assist import graph
    import dataclasses as dc
    monkeypatch.setattr(graph, "CFG", dc.replace(graph.CFG, runs_dir=tmp_path))
    monkeypatch.setattr(graph, "run_copy", lambda prof, dest, code=None: dest)
    monkeypatch.setattr(graph.context, "collect", lambda *a: (_ for _ in ()).throw(
        testwriter.WriterRefused("no source file contains any exact string from the issue")))
    out = graph.gather_context({"run_id": "r", "profile": {"repo": "vercel/ai"}, "issue": {"title": "t"}})
    assert out["outcome"]["exit"] == "CONTEXT NOT FOUND"
    from debug_assist import plain
    assert plain.exit_text("CONTEXT NOT FOUND") == "Stopped. It could not find any code that matches the issue."


def test_a_fix_pull_request_named_in_the_discussion_is_found_when_the_timeline_shows_none(monkeypatch):
    """Review of run #22543 (2026-10-10): "0 linked" while comment 3 named the open fix PR #22545."""
    texts = ["see #12 and https://github.com/o/r/pull/45, and o/r#99 &#38;", "same as #1 (this issue)"]
    assert context.mentioned("o", "r", 1, texts) == [12, 45]
    prs = {12: {"title": "unrelated", "body": "about something else", "state": "closed"},
           45: {"title": "fix: keep entries", "body": "Fixes #1. thanks @octocat", "state": "open", "created_at": "2026-10-10T03:22:54Z"}}
    def api(path):
        n = int(path.split("/pulls/")[1].split("/")[0])
        if path.endswith("/files?per_page=30"):
            return [{"filename": "packages/vue/src/use-object.ts", "patch": "-  ...(headers as any),\n+  ...normalizeHeaders(headers),"}]
        return prs.get(n)
    monkeypatch.setattr(context.github_read, "api", api)
    got = context.fix_prs("o", "r", 1, [], texts, best="packages/vue/src/use-object.ts")
    assert [(p["number"], p["state"], p["opened"]) for p in got] == [(45, "open", "2026-10-10")]   # #12 doesn't name #1
    assert "normalizeHeaders" in got[0]["diff"]
    pack = {"issue": {"comments": [], "linked": [], "fix_prs": got, "errors": {"errors": [], "frames": []}},
            "related": [], "history": [], "code": {"ranking": [], "parallels": [], "helpers": []}}
    assert "PULL REQUESTS FOR THIS ISSUE" in context.brief(pack)[0] and "never copy it blindly" in context.brief(pack)[0]


def test_the_fixer_sees_the_same_file_in_other_packages_and_the_helpers_it_can_import(tmp_path):
    """Review of run #22543: react's useObject already used the shared normalizeHeaders; the fix wrote its own."""
    from debug_assist.profiles import PROFILES
    files = {"packages/vue/package.json": '{"name": "@x/vue", "dependencies": {"@x/utils": "workspace:*"}}',
             "packages/vue/src/use-object.ts": "export function useObject(headers) {\n  return { ...(headers as any) };\n}\n",
             "packages/react/package.json": '{"name": "@x/react"}',
             "packages/react/src/use-object.ts": "export function useObject(headers) {\n  return normalizeHeaders(headers);\n}\n",
             "packages/utils/package.json": '{"name": "@x/utils"}',
             "packages/utils/src/normalize-headers.ts": "export function normalizeHeaders(h) {\n  return h;\n}\n",
             "packages/otel/package.json": '{"name": "@x/otel"}',
             "packages/otel/src/span-headers.ts": "export function spanHeaders(h) {\n  return h;\n}\n"}
    for p, t in files.items():
        (tmp_path / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / p).write_text(t)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    prof = PROFILES["vercel/ai"]
    focus = "`useObject` accepts `headers?: Record<string, string> | Headers` but drops them"
    par = context.parallels(tmp_path, "packages/vue/src/use-object.ts", focus, prof)
    assert [p["path"] for p in par] == ["packages/react/src/use-object.ts"] and "normalizeHeaders" in par[0]["snippets"]
    got = [h["name"] for h in context.helpers(tmp_path, focus, "packages/vue/src/use-object.ts", prof)]
    assert got == ["normalizeHeaders"]       # not useObject itself, not otel's (vue can't import it)


def test_a_name_imported_from_outside_the_repo_is_not_matched_to_a_same_named_definition(tmp_path):
    """Review of run #22543: vue's `ref` was shown as shared code from code-mode's `const ref`."""
    from types import SimpleNamespace
    from debug_assist.profiles import PROFILES
    files = {"packages/vue/package.json": '{"name": "@x/vue"}',
             "packages/vue/src/use-object.ts": "import { ref } from 'vue';\nexport function useObject() {\n  const a = ref(1);\n}\n",
             "packages/code/package.json": '{"name": "@x/code"}',
             "packages/code/src/prompt.ts": "export const ref = (x) => x;\n"}
    for p, t in files.items():
        (tmp_path / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / p).write_text(t)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    ctx = SimpleNamespace(source="packages/vue/src/use-object.ts", snippets="   3  const a = ref(1);")
    assert context.related(tmp_path, ctx, PROFILES["vercel/ai"]) == []
