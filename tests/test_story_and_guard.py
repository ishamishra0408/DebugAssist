"""why_it_shipped and lasting_guard: evidence gathered by code, stories and guards checked by code."""
import base64
import subprocess
from types import SimpleNamespace

import pytest

from debug_assist import guard, story
from debug_assist.guardrails import GuardrailViolation

PATCH = """--- a/x.ts
+++ b/x.ts
@@ -1,3 +1,5 @@
-            toolCallTracker.flush();
+            if (finishReason != null) {
+              toolCallTracker.flush();
+            }
             // a comment
"""


def test_the_story_traces_the_line_the_fix_removed():
    assert story.signature_lines(PATCH) == ["toolCallTracker.flush();"]
    assert story.blank_handles("approved by @jdoe; see a@b.com") == "approved by @someone; see a@b.com"


def fake_github(monkeypatch, versions, prs):
    """versions: newest-first list of (sha, file text); prs: sha → PR dict."""
    history = [{"sha": sha, "commit": {"committer": {"date": f"2026-0{9 - i}-01T00:00:00Z"}, "message": f"commit {sha}"}}
               for i, (sha, _) in enumerate(versions)]
    text = dict(versions)

    def api(path):
        if "/commits?path=" in path:
            return history if "page=1" in path else []
        if "/contents/" in path:
            sha = path.split("ref=")[1]
            return {"content": base64.b64encode(text[sha].encode()).decode()}
        if path.endswith("/pulls") and "/commits/" in path:
            sha = path.split("/commits/")[1].split("/")[0]
            return [prs[sha]] if sha in prs else []
        if "/commits/" in path:
            sha = path.rsplit("/", 1)[1]
            i = [v[0] for v in versions].index(sha)
            older = versions[i + 1][1] if i + 1 < len(versions) else ""
            changed = ("toolCallTracker.flush();" in text[sha]) != ("toolCallTracker.flush();" in older)
            return {"files": [{"filename": "packages/p/src/x.ts",
                               "patch": "+  toolCallTracker.flush();" if changed else "+  other"}]}
        if path.endswith("/reviews?per_page=100"):
            return [{"state": "APPROVED", "user": {"login": "reviewer1"}}]
        if path.endswith("/comments?per_page=100"):
            return []
        if path.endswith("/files?per_page=100"):
            return [{"filename": "packages/p/src/x.test.ts"}]
        return None
    monkeypatch.setattr(story, "api", api)


def _pr(n, login):
    return {"number": n, "title": f"PR {n}", "merged_at": "2026-04-17T00:00:00Z", "created_at": "2026-04-16T00:00:00Z",
            "body": "cc @someoneelse: flush finalizes calls", "user": {"login": login}}


def test_gather_finds_where_the_line_was_written_and_never_shows_names(monkeypatch, tmp_path):
    versions = [("c4", "toolCallTracker.flush();"), ("c3", "toolCallTracker.flush();"), ("c2", "toolCallTracker.flush();"),
                ("c1", "old code")]
    fake_github(monkeypatch, versions, {"c2": _pr(14565, "author1")})
    issue = {"owner": "o", "repo": "r", "number": 21439, "reporter": "reporter1", "labels": ["bug"], "created_at": "2026-09-24"}
    ev = story.gather(issue, tmp_path, "packages/p/src/x.ts", PATCH)
    assert ev["written"]["commit"].startswith("c2") and ev["written"]["pr"]["number"] == 14565
    assert ev["written"]["pr"]["reviews"] == 1 and ev["written"]["pr"]["tests_changed"]
    assert "@someone:" in ev["written"]["pr"]["description"] and "someoneelse" not in str(ev)
    assert {"author1", "reviewer1", "reporter1"} <= ev["_names"] and "_names" not in ev["written"]["pr"]


GOOD = """### What broke?
A tool call cut off mid-stream was reported as complete.
### When did the faulty code arrive, and what was that change for?
#14565 on 2026-04-17 moved the tracker into shared code so every provider could use it.
### What did it assume that was not true?
That a stream always ends with a finish reason.
### Why didn't the tests catch it?
The tests fed whole streams only; none cut a stream mid tool call.
### Why didn't review or the release catch it?
One review, no comments on stream endings, and the change came with tests for whole streams only.
### How long was it out before it was reported, and why so long?
Not known: the release that first shipped it is not in the changelog read.
### Which conditions, together, let it ship?
C1 — flush had no input for a broken stream.
C2 — no test cut a stream mid tool call.
### What couldn't be found out?
- renames not followed
CONDITION: the finalizer could not tell a finished stream from a broken one"""


def test_the_story_check_refuses_names_inventions_and_thin_stories():
    ev = {"issue": {"number": 21439}, "written": {"pr": {"number": 14565}}, "shaped": []}
    assert story.check(GOOD, ev, {"author1"}).startswith("the finalizer")
    with pytest.raises(GuardrailViolation, match="names people"):
        story.check(GOOD + "\nthanks author1", ev, {"author1"})
    with pytest.raises(story.StoryRefused, match="#16838"):
        story.check(GOOD.replace("#14565", "#16838"), ev, set())
    with pytest.raises(story.StoryRefused, match="two conditions"):
        story.check(GOOD.replace("C2 —", "Also,"), ev, set())
    with pytest.raises(story.StoryRefused, match="CONDITION"):
        story.check(GOOD.rsplit("CONDITION", 1)[0], ev, set())


def test_the_story_answers_every_template_question_in_order():
    ev = {"issue": {"number": 21439}, "written": {"pr": {"number": 14565}}, "shaped": []}
    assert all(f"### {q}" in story.SYSTEM for q, _ in story.QUESTIONS)          # the prompt asks exactly these
    assert [q for q, _ in story.answers(GOOD)] == [q for q, _ in story.QUESTIONS]
    with pytest.raises(story.StoryRefused, match="not answered under its own heading: Why didn't the tests catch it"):
        story.check(GOOD.replace("### Why didn't the tests catch it?\n", ""), ev, set())
    with pytest.raises(story.StoryRefused, match="no answer under: What broke"):
        story.check(GOOD.replace("A tool call cut off mid-stream was reported as complete.\n", ""), ev, set())
    swapped = GOOD.replace("### What broke?", "### TMP").replace("### What did it assume that was not true?", "### What broke?")
    with pytest.raises(story.StoryRefused, match="order"):
        story.check(swapped.replace("### TMP", "### What did it assume that was not true?"), ev, set())


def test_tell_retries_once_with_the_refusal_then_gives_up(monkeypatch, tmp_path):
    monkeypatch.setattr(story, "gather", lambda *a: {"issue": {"number": 1}, "written": None, "shaped": [],
                                                     "_names": {"author1"}, "line": "x", "stops": []})
    seen = []
    monkeypatch.setattr(story, "write", lambda st, step, msgs, max_tokens: (
        seen.append(msgs[-1][1]) or SimpleNamespace(content=GOOD + "\nby author1"), {}))
    with pytest.raises(story.StoryRefused, match="no acceptable story"):
        story.tell({"issue": {"title": "t"}}, tmp_path, {"file": "f", "lines": [1, 2]}, PATCH)
    assert len(seen) == 2 and "REFUSED" in seen[1] and "names people" in seen[1]


# ── guard ────────────────────────────────────────────────────────────────────────────────────────
def test_siblings_are_the_same_line_elsewhere(tmp_path):
    for p in ("packages/a/src/m.ts", "packages/b/src/m.ts", "packages/b/src/m.test.ts"):
        (tmp_path / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / p).write_text("x\n  toolCallTracker.flush();\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    assert guard.sibling_sites(tmp_path, "toolCallTracker.flush();", "packages/a/src/m.ts") == ["packages/b/src/m.ts:2"]


def test_bare_lines_are_not_patterns():
    """Opus run 2026-10-07: `index,` and `});` from a fix matched hundreds of files."""
    patch = "-    index,\n-  });\n-  id: pending.id,\n-  toolCallTracker.flush();\n-  // toolCallTracker.flush();\n"
    assert story.signature_lines(patch) == ["toolCallTracker.flush();"]


def test_vitest_cases_are_read_per_case():
    out = " ✓ guard > no finish reason 3ms\n × guard > error mid-stream 4ms\n × guard > length limit\n"
    assert guard.cases(out) == {"passed": ["guard > no finish reason"], "failed": ["guard > error mid-stream", "guard > length limit"]}


def _guard_env(tmp_path, on_unfixed):
    fixed, unfixed, keep = tmp_path / "fixed", tmp_path / "unfixed", tmp_path / "keep"
    for d in (fixed, unfixed):
        (d / "packages/p/src").mkdir(parents=True)
    (fixed / "packages/p/src/judge.test.ts").write_text("expect(1)")
    profile = SimpleNamespace(env="", test_cmd="cd packages/{package} && pnpm test:node {test_path}", image="n",
                              language="typescript")

    def run(cmd, workdir, network, timeout, image):
        if "unfixed" in str(workdir):
            return SimpleNamespace(returncode=1 if on_unfixed == "RED" else 0, stderr="",
                                   stdout=" × guard > no finish reason\n FAIL  src/g.test.ts > guard > no finish reason\nAssertionError: expected [ { type: 'tool-call' } ] to strictly equal []\n 1 failed"
                                   if on_unfixed == "RED" else " ✓ guard > no finish reason")
        return SimpleNamespace(returncode=1, stderr="", stdout=(
            " ✓ guard > no finish reason\n × guard > length limit\n × guard > closed by server\n"
            " FAIL  src/g.test.ts > guard > length limit\nAssertionError: expected [ { type: 'tool-call' } ] to strictly equal []\n"
            " FAIL  src/g.test.ts > guard > closed by server\nError: ENOENT: no such file or directory, open 'x.sse'\n"))
    return fixed, unfixed, keep, profile, run


REPLY = "COVERS: every way a stream can end mid tool call\n```ts\nit.each([1])('x', () => expect([]).toStrictEqual([]));\n```"
STATE = {"issue": {"number": 7, "title": "t"}, "focus": "flush emits a complete-looking `tool-call` part"}
CAUSE = {"file": "packages/p/src/m.ts", "lines": [1, 2], "why": "w"}


def test_a_guard_must_catch_the_bug_and_reports_what_the_fix_left_open(tmp_path, monkeypatch):
    fixed, unfixed, keep, profile, run = _guard_env(tmp_path, "RED")
    monkeypatch.setattr(guard, "write", lambda *a, **k: (SimpleNamespace(content=REPLY), {}))
    g = guard.write_guard(STATE, profile, fixed, unfixed, "packages/p/src/judge.test.ts", CAUSE, PATCH, "c", "", "", keep, run_cmd=run)
    assert g["status"] == "CATCHES THE BUG" and g["on_fixed"]["failed"] == ["guard > length limit"]
    assert len(g["on_fixed"]["broken"]) == 1 and "ENOENT" in g["on_fixed"]["broken"][0]  # never reported as "open"
    assert (keep / "da-guard-7.test.ts").exists() and not (fixed / g["repo_path"]).exists()  # kept out of the fix


def test_a_guard_that_does_not_fail_on_the_old_code_is_not_a_guard(tmp_path, monkeypatch):
    fixed, unfixed, keep, profile, run = _guard_env(tmp_path, "GREEN")
    monkeypatch.setattr(guard, "write", lambda *a, **k: (SimpleNamespace(content=REPLY), {}))
    g = guard.write_guard(STATE, profile, fixed, unfixed, "packages/p/src/judge.test.ts", CAUSE, PATCH, "c", "", "", keep, run_cmd=run)
    assert g["status"] == "NOT WRITTEN" and "UNFIXED" in g["why"] and len(g["tries"]) == guard.GUARD_TRIES


def test_a_recently_reworded_line_is_traced_back_through_the_call_itself(monkeypatch, tmp_path):
    """Trial 2026-10-07: the exact line was last reworded by a later PR; the behaviour began earlier."""
    versions = [("c4", "toolCallTracker.flush();"), ("c3", "toolCallTracker.flush();"),
                ("c2", "toolCallTracker.flush(controller);"), ("c1", "old code")]
    fake_github(monkeypatch, versions, {"c2": _pr(14565, "a1"), "c3": _pr(14755, "a2")})
    issue = {"owner": "o", "repo": "r", "number": 21439, "reporter": "r1", "labels": []}
    ev = story.gather(issue, tmp_path, "packages/p/src/x.ts", PATCH)
    assert ev["written"]["pr"]["number"] == 14565 and ev["line"] == "toolCallTracker.flush("
    assert any("oldest origin" in s for s in ev["stops"])


def test_cases_that_share_one_error_block_are_judged_by_it():
    """vitest prints identical failures as consecutive FAIL headers over one shared error."""
    out = (" × a 3ms\n × b 2ms\n FAIL  f.test.ts > d > a\n FAIL  f.test.ts > d > b\n"
           "AssertionError: expected [ { type: 'tool-call' } ] to strictly equal []\n")
    j = guard.judged("emits a `tool-call` part", out)
    assert j["symptom"] == ["a", "b"] and j["broken"] == []


def test_the_story_starts_from_the_oldest_of_all_lines_the_fix_changed(monkeypatch, tmp_path):
    """Opus run 2026-10-07: the fix changed two lines; tracing only the first missed the older origin."""
    patch = "-  for (const p of pending) {\n-  toolCallTracker.flush();\n+  if (done) {\n"
    versions = [("c3", "for (const p of pending) {\ntoolCallTracker.flush();"), ("c2", "toolCallTracker.flush();"), ("c1", "old")]
    fake_github(monkeypatch, versions, {"c2": _pr(14565, "a1"), "c3": _pr(14760, "a2")})
    ev = story.gather({"owner": "o", "repo": "r", "number": 1, "reporter": "r1", "labels": []}, tmp_path, "packages/p/src/x.ts", patch)
    assert ev["written"]["pr"]["number"] == 14565 and "also traced" in " ".join(ev["stops"])


def test_case_names_come_back_unescaped():
    assert guard.cases(" ✓ f.test.ts &gt; d &gt; case 2ms\n") == {"passed": ["f.test.ts > d > case"], "failed": []}
