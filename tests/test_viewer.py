"""The run viewer: read-only, escapes everything from outside, refreshes only while a run is live."""
import json
import re
from debug_assist import viewer


def _data(**over):
    d = {"run_id": "ai-1-x", "next": ["approval"], "interrupt": {"sha256": "abc123def456", "pr_body_path": "/x/PR.md"},
         "pr_text": "## Fix\n<script>alert(1)</script>", "pr_matches": True, "patch": "--- a/x\n+++ b/x\n-old\n+new",
         "events": [{"at": "2026-10-07T06:20:00+00:00", "step": "reproduce", "kind": "attempt", "rung": "unit", "n": 1, "outcome": "RED"}],
         "meter": {"spent_usd": 0.69, "cap_usd": 2.5, "sandbox_used_s": 49, "sandbox_cap_s": 1800}, "calls": [], "built": "now",
         "now": "2026-10-07T06:50:00+00:00", "trials": {"ai-1-x-refix": {"n": 3, "kinds": ["fix_attempt"], "last": "x"}},
         "state": {"issue": {"repo": "ai", "number": 1, "title": "<img src=x onerror=alert(1)>"}, "issue_url": "u",
                   "triage": {"x": 1}, "repro": {"status": "REPRODUCED", "rung": "unit", "confirmed": True},
                   "attempts": [{"step": "reproduce", "n": 1, "rung": "unit", "outcome": "RED", "evidence": "AssertionError: x", "test_path": "a/b.test.ts"}],
                   "fix_clock": {"seconds": 204.3, "validated": True, "judges": 2},
                   "second_story": {"text": "## Critical junctures\nC1 — x"},
                   "log": ["read_issue: triaged", "reproduce: REPRODUCED"]}}
    d.update(over)
    return d


def test_outside_text_is_escaped_and_markdown_is_sanitised():
    page = viewer.render(_data())
    assert "<img src=x onerror" not in page and "&lt;img src=x onerror=alert(1)&gt;" in page
    assert "DOMPurify.sanitize(marked.parse" in page and "<\\/script>" in page  # the PR text can't close the data block


def test_the_page_only_copies_the_approve_command_and_refreshes_only_while_live():
    page = viewer.render(_data())
    assert "uv run debug-assist approve ai-1-x" in page and "<form" not in page and "fetch(" not in page
    assert 'http-equiv="refresh"' not in page                                  # paused for approval: not live
    running = viewer.render(_data(interrupt={}, next=["write_fix"]))
    assert 'http-equiv="refresh"' in running and "approve ai-1-x" not in running


def test_steps_read_from_the_state():
    rows = {r["key"]: r["status"] for r in viewer.step_rows(_data())}
    assert rows["read_issue"] == "done" and rows["reproduce"] == "done" and rows["approval"] == "waiting for you"
    stopped = _data(state={**_data()["state"], "outcome": {"exit": "FIX NOT VALIDATED", "why": "w"},
                           "log": ["read_issue: x", "reproduce: x", "write_fix: STOPPED FIX NOT VALIDATED"]}, next=[], interrupt={})
    r2 = {r["key"]: r["status"] for r in viewer.step_rows(stopped)}
    assert r2["write_fix"] == "stopped" and r2["why_it_shipped"] == "not reached"


def test_freshness_and_trials_are_shown():
    page = viewer.render(_data())
    assert 'data-since="2026-10-07T06:20:00+00:00" data-suffix=" ago"' in page  # the browser counts "N min ago"
    assert "ai-1-x-refix: 3 events" in page and "never counted in the north stars" in page
    assert "NOT SCORED" in page


def test_the_page_marks_the_step_we_are_on_in_plain_words():
    st = {**_data()["state"], "triage": {"x": 1}, "cause": {"file": "f"}}
    live = viewer.render(_data(state=st, interrupt={}, next=["write_fix"], since="2026-10-07T06:49:28+00:00"), mode="live")
    assert 'class="status s-live"' in live and "Working on step 5 of 10: Fix it" in live
    assert 'data-since="2026-10-07T06:49:28+00:00"' in live          # the running step's clock, counted by the browser
    assert live.count('class="node running"') == 1 and live.count('class="row running"') == 1 and 'class="i spin"' in live
    assert "DebugAssistAgent" in live and "Nothing to do right now" in live
    paused = viewer.render(_data(), mode="live")
    assert 'class="status s-waiting"' in paused and "Waiting for your OK" in paused and 'class="node waiting"' in paused
    assert 'popovertarget="check-pr"' in paused and "Check PR" in paused and "Nothing is posted to GitHub." in paused
    assert 'data-decide="approve"' in paused and 'data-decide="reject"' in paused and "Confirm approve" in paused
    stopped = viewer.render(_data(state={**st, "outcome": {"exit": "FIX NOT VALIDATED", "why": "w"},
                                         "log": ["write_fix: STOPPED FIX NOT VALIDATED"]}, next=[], interrupt={}), mode="live")
    assert 'class="status s-stopped"' in stopped and "Stopped at step 5 of 10: Fix it" in stopped
    assert "None of its fixes passed the tests." in stopped and 'class="node stopped"' in stopped


def test_glass_is_kept_to_controls_and_navigation():
    """HIG: Liquid Glass is the layer for controls and navigation; content sits on solid surfaces (no glass on glass)."""
    page = viewer.render(_data(), mode="live")
    glassy = re.findall(r'<(\w+)[^>]*class="([^"]*\bglass\b[^"]*)"', page)
    assert glassy and all(tag in ("div", "aside", "button", "a") for tag, _ in glassy)
    assert all(any(k in c for k in ("tgroup", "dock", "btn", "seg", "pop")) for _, c in glassy)
    assert 'class="sect' in page and "glass" not in re.search(r'class="sect[^"]*"', page).group(0)


def test_no_jargon_reaches_the_operator_outside_the_engineer_details():
    st = {**_data()["state"], "triage": {"x": 1}, "cause": {"file": "f"}}
    page = viewer.render(_data(state=st, interrupt={}, next=["write_fix"]), mode="live")
    operator = re.sub(r"<script.*?</script>", "", page.split('<details class="eng">')[0], flags=re.S)
    operator = re.sub(r"<style>.*?</style>", "", operator, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", operator)
    for word in ("REPRODUCED", "VALIDATED", "rung", "sandbox", "holdout", "oracle", "judge", "checkpoint", "laya",
                 "north star", "back-test", "PLACEHOLDER", "trial"):
        assert word.lower() not in text.lower(), word


def test_inside_the_step_shows_what_has_happened_not_a_guess():
    evs = [{"step": "write_fix", "kind": "model_call"}, {"step": "write_fix", "kind": "sandbox"},
           {"step": "write_fix", "kind": "fix_attempt", "n": 1, "ok": False},
           {"step": "write_fix", "kind": "fix_attempt", "n": 2, "ok": True},
           {"step": "write_fix", "kind": "holdout", "on_fixed": "GREEN"}, {"step": "reproduce", "kind": "sandbox"}]
    chips, counts = viewer.inside("write_fix", evs)
    assert chips == ["Fix try 1: did not pass", "Fix try 2: passed all tests", "Second test: passes on the fix"]
    assert counts == "1 AI call · 1 test run"


def test_served_pages_update_in_place_and_files_stay_static():
    served = viewer.render(_data(interrupt={}, next=["write_fix"]), mode="live")
    assert 'http-equiv="refresh"' not in served and "fetch(u" in served and 'data-k="hero"' in served
    assert 'data-final="0"' in served
    still = viewer.render(_data(interrupt={}, next=["write_fix"]))
    assert "fetch(" not in still and 'http-equiv="refresh"' in still
    rp = viewer.render(_data(replay=True), mode="replay", replay={"speed": 8, "elapsed": 3, "length": 40, "final": False})
    assert "Replay of a past run" in rp and "3 of 40 s" in rp and "Nothing is running." in rp


def test_a_run_that_has_not_started_yet_renders_as_getting_ready():
    page = viewer.render(_data(state={}, next=[], interrupt={}, events=[], pr_text="", patch=""), mode="live")
    assert "Getting ready" in page and "Nothing yet" in page and 'data-final="0"' in page
    failed = viewer.render(_data(state={}, next=[], interrupt={}, events=[], pr_text="", patch="",
                                 console="PREFLIGHT FAIL   Docker          daemon not running\n"), mode="live")
    assert "Could not start" in failed and "What needs fixing" in failed and "Docker          daemon not running" in failed
    assert 'data-final="1"' in failed


def test_a_stopped_run_says_no_fix_and_small_spend_shows():
    st = {**_data()["state"], "fix_clock": {"started_at": "x"}, "outcome": {"exit": "NEVER REPRODUCED", "why": "w"}}
    page = viewer.render(_data(state=st, next=[], interrupt={}, meter={"spent_usd": 0.00431, "cap_usd": 0.5}), mode="live")
    assert "<b>No fix</b>" in page and "<b>$0.0043</b><small>Budget $0.50</small>" in page
    assert "It could not make the bug happen, so it did not try to fix it." in page


def test_a_run_that_went_quiet_reads_interrupted_with_the_command_to_continue():
    st = {**_data()["state"], "triage": {"x": 1}, "cause": {"file": "f"}}
    page = viewer.render(_data(state=st, interrupt={}, next=["write_fix"], since="2026-10-07T05:00:00+00:00",
                               now="2026-10-07T06:50:00+00:00"), mode="live")
    assert "Interrupted during step 5 of 10: Fix it" in page and "uv run debug-assist resume ai-1-x" in page


def test_the_state_chart_walks_this_run_and_stops_where_it_stopped():
    from debug_assist import chart
    st = {**_data()["state"], "triage": {"is_defect_p": .98}, "context": {"status": "GATHERED", "counts": {
          "comments": 5, "files": 5, "related": 3, "changes": 11}},
          "attempts": [{"step": "reproduce", "rung": "unit", "n": 1, "outcome": "ERROR"},
                       {"step": "reproduce", "rung": "unit", "n": 2, "outcome": "ERROR"}],
          "repro": {"status": "NEVER REPRODUCED"}, "outcome": {"exit": "NEVER REPRODUCED", "why": "w"}}
    path = chart.this_run(_data(state=st, interrupt={}, next=[]))
    assert [p[0] for p in path] == ["ce-issue", "ce-triaged", "ce-try", "ce-try", "ce-stop-context"]
    assert path[-1][1] == "stop-context" and path[-1][2].startswith("Stopped. It could not make the bug happen")
    assert path[2][2] == "Try 1 (quick test): test broke for another reason. Trying again."
    every = chart.every_path()
    assert every[-1][1] == "ready" and any(p[0] == "ce-round2" for p in every)   # the tour shows each retry once
    svg = chart.svg()
    assert all(f'id="cn-{s}"' in svg for s, _ in chart.STATES) and all(f'id="ce-stop-{s}"' in svg for s in chart.STOPS)
    page = viewer.render(_data(state=st, interrupt={}, next=[]), mode="live")
    assert "How this run moved" not in page and 'data-k="chart"' not in page        # the chart lives on How it works


def test_latest_activity_is_a_pop_up_and_usage_uses_standard_names():
    evs = [{"at": "2026-10-08T10:00:0%dZ" % i, "step": "reproduce", "kind": "model_call", "input_tokens": 1500,
            "output_tokens": 200} for i in range(3)]
    page = viewer.render(_data(events=evs, interrupt={}, next=["reproduce"]), mode="live")
    assert 'popovertarget="acts"' in page and '<div id="acts" popover class="pop gh gh-menu"' in page   # GitHub's way
    body = page.split("<main>")[1].split("</main>")[0]
    assert "Latest activity" not in body and 'data-k="log"' not in body           # not a section on the page any more
    assert re.search(r'class="badge"[^>]*>3<', page)
    for word in ("Usage and cost", "Time to fix", "LLM cost", "Tokens", "4.5k in · 600 out", "3 model calls", "Compute time",
                 "Last event"):
        assert word in page, word
    assert "Time and money" not in page and "Money spent" not in page


def _sheet(page: str, id_: str) -> str:
    """One pop-up sheet's markup: from its opening to the next sheet."""
    return re.split(r'<div id="[\w-]+" popover', page.split(f'<div id="{id_}" popover')[1])[0]


def test_show_the_bug_carries_its_proof(tmp_path, monkeypatch):
    """Isha 2026-10-08: first, does a test already in the repo fail for this issue; if not, the test written at each
    level (unit, integration, automation), shown failing on the unfixed code and passing after the fix; then every try
    as a page of its own."""
    from types import SimpleNamespace

    from debug_assist import plain, testwriter
    monkeypatch.setattr(viewer, "_runs_dir", lambda: tmp_path)
    rd = tmp_path / "ai-1-x"
    (rd / "checkout/packages/p/src").mkdir(parents=True)
    (rd / "checkout/packages/p/src/da-repro-1-integration-3.test.ts").write_text("it('confirms', () => expect(parts).toEqual([]))")
    (rd / "attempt-tests").mkdir()
    (rd / "attempt-tests/da-repro-1-unit-2.test.ts").write_text("it('shows', () => expect(parts).toEqual([]))")
    prof = SimpleNamespace(repo="vercel/ai", base_commit="e7f55a481fe2c3", image="node:22")
    testwriter.write_proof(rd / "proof", "packages/p/src/da-repro-1-unit-2.test.ts", "it('shows')", "pnpm test:node x",
                           1, "FAIL x\nAssertionError: expected [ { type: 'tool-call' } ] to strictly equal []", "RED", "x",
                           prof, rd / "checkout")
    unit, integ = "packages/p/src/da-repro-1-unit-2.test.ts", "packages/p/src/da-repro-1-integration-3.test.ts"
    st = {**_data()["state"],
          "repro": {"status": "REPRODUCED", "confirmed": True, "failing_test": unit, "oracle_test": integ,
                    "existing_tests": {"status": "NONE FAIL", "package": "packages/p", "passed": 412, "failed": 0},
                    "ladder_plan": {"rungs": ["unit", "integration"], "skipped": {"end_to_end": "needs live keys"}}},
          "fix": {"status": "VALIDATED", "after_fix": [{"test_path": unit, "outcome": "GREEN"}, {"test_path": integ, "outcome": "GREEN"}]},
          "attempts": [{"step": "reproduce", "n": 1, "rung": "unit", "outcome": "ERROR", "test_path": "packages/p/src/da-repro-1-unit-1.test.ts",
                        "evidence": "RED for another reason: x"},
                       {"step": "reproduce", "n": 2, "rung": "unit", "outcome": "RED", "test_path": unit,
                        "evidence": "AssertionError: expected [ { type: 'tool-call' } ] to strictly equal []\nwriter's symptom: emits a tool-call"},
                       {"step": "reproduce", "n": 3, "rung": "integration", "outcome": "RED", "test_path": integ, "evidence": "AssertionError: same"}]}
    page = viewer.render(_data(state=st), mode="live")
    assert 'popovertarget="proof"' in page and "Check proof" in page and '<div id="proof" popover' in page
    sheet = _sheet(page, "proof")
    text = re.sub(r"<[^>]+>", " ", sheet)
    # GitHub's way (Isha 2026-10-08): a workflow run, its Summary of checks in Isha's order (the repo's own tests
    # first, then what was written at each level), then every try as a job
    assert '<h3 class="gh-title">Reproduce issue <span class="gh-muted">#1</span></h3>' in sheet and "Bug shown</span> on" in sheet
    qs = re.findall(r'class="gh-cname"><b>([^<]+)<', sheet)
    assert qs == ["Test already in the repo failing for this issue?", "Unit test failing for this issue?",
                  "Integration test failing for this issue?", "Automation test (end to end) failing for this issue?"]
    assert "All 412 of the repo&#x27;s own tests in packages/p pass on main" in sheet
    assert re.search(r"Unit test failing.*?Written for this issue \(try 2\).*?Successful with the change.*?Found", sheet, re.S)
    assert re.search(r"Integration test failing.*?on real recorded data.*?Successful with the change.*?Found", sheet, re.S)
    assert re.search(r"Automation test.*?live API keys.*?Skipped", sheet, re.S)
    # every try is a job, turned by number; the pager opens on the try that showed the bug
    assert sheet.count('class="page gh-job"') == 3 and re.findall(r'data-go="(\d)"', sheet) == ["0", "1", "2"]
    assert 'data-start="1"' in sheet and "Failed, but for another reason" in text and text.count("Failing on main: shows the bug") == 2
    assert "What the test checks:</span> emits a tool-call" in sheet
    # the test as a change to the code, git style; the run as GitHub Actions shows it
    assert 'class="dfile"' in sheet and "it('shows')" in sheet and "it('confirms'" in sheet and 'class="dadd"' in sheet
    assert "<summary>Run pnpm test:node x</summary>" in sheet and "Runner: Docker container from node:22, internet off" in sheet
    assert '<tr class="err"><td class="ln">2</td><td>AssertionError' in sheet and "Set up job" in sheet and "Complete job" in sheet
    assert "✓" not in sheet and "&#10003;" not in sheet                      # no tick marks
    assert "Not run: the draft was refused before it ran" not in sheet       # try 1 ran: it says what it printed
    assert plain.result("reproduce", st) == "Bug shown on try 2, then confirmed on a recorded stream (try 3)"
    old = {**st, "repro": {**st["repro"], "existing_tests": None}, "fix": {}}
    sheet = _sheet(viewer.render(_data(state=old), mode="live"), "proof")
    assert "Not checked" in sheet and "Expected: waiting for the fix" in sheet
    no = viewer.render(_data(state={**st, "repro": {"status": "NEVER REPRODUCED"}}), mode="live")
    assert "Check proof" not in no and '<div id="proof" popover' in no   # the sheet is always there


def test_a_proof_button_added_by_a_live_update_has_a_sheet_to_open():
    """2026-10-08, live run of #22288: the page was opened while the bug was not shown yet; the update added the
    button, but the sheet existed only on pages built after the proof, so the button opened nothing."""
    st = {**_data()["state"], "repro": {"status": "RUNNING"}, "attempts": []}
    before = viewer.render(_data(state=st, interrupt={}, next=["reproduce"]), mode="live")
    assert '<div id="proof" popover' in before and 'data-k="proof"' in before and "Not shown yet." in before


def test_an_advisors_full_answer_opens_from_its_row_in_plain_sections():
    """Isha 2026-10-08: show what the advisor's answer gave in full, in a collapsible dialog."""
    raw = {"seat": "allspaw-debugassist", "judgment_id": "judg_1", "verdict": "READY_FOR_JUDGMENT",
           "machine_result": {"state": "READY_FOR_JUDGMENT", "blame_sentences": ["<b>The reviewer should have caught it.</b>"],
                              "checks": [{"id": "blame_scan", "passed": False, "detail": "1 sentence blames a person"},
                                         {"id": "required_fields", "passed": True, "detail": "present"}],
                              "rules": ["no blame language: condition is a property of system, never a person"]},
           "falsifiable_test": {"statement": "The guard prevents the incident class.", "resolve_rule": "PASS if no recurrence in 30 days",
                                "state": "delivered", "test_id": "test_1"},
           "calibration": {"judgments": 3, "resolved": 0, "passed": 0, "failed": 0, "pass_rate_note": "n=3 < 30"}}
    st = {**_data()["state"], "advisors": {"why_it_shipped": {"status": "ANSWERED", "seat": "allspaw",
                                                               "answer": "Blame check: 1 sentence…", "raw": raw}}}
    page = viewer.render(_data(state=st), mode="live")
    assert 'popovertarget="advfull-why_it_shipped"' in page and '<div id="advfull-why_it_shipped" popover' in page
    sheet = page.split('id="advfull-why_it_shipped"')[1]
    for part in ("Verdict", "Checks", "Findings", "Rules", "How this answer will be tested",
                 "Track record", "Raw response (JSON)", "blame scan", "3 answers, 0 checked", "Failed", "Passed",
                 '<span class="Label Label--accent">Incident review</span>', 'class="adv-seal pal-ember"'):
        assert part in sheet, part
    assert "<b>The reviewer" not in sheet and "&lt;b&gt;The reviewer" in sheet          # the server's text is escaped
    assert sheet.count("<details") >= 7                                                 # each section folds
    no_raw = {**st, "advisors": {"why_it_shipped": {"status": "ANSWERED", "answer": "x"}}}
    assert "Full answer" not in viewer.render(_data(state=no_raw), mode="live")         # older answers: no button


def test_check_pr_binds_your_ok_to_the_text_shown_and_a_saved_file_cannot_decide():
    """Isha 2026-10-08: the PR as GitHub shows it: title, Conversation, Commits (editable message), Files changed (git
    style); Approve / Say no with a second tap; no tick marks. A saved file shows the terminal commands instead."""
    patch = ("diff --git a/packages/ai/src/ui/chat.ts b/packages/ai/src/ui/chat.ts\n--- a/packages/ai/src/ui/chat.ts\n"
             "+++ b/packages/ai/src/ui/chat.ts\n@@ -10,3 +10,3 @@ class Chat\n keep\n-old line\n+new line\n"
             "diff --git a/packages/ai/src/ui/da-repro-1-unit-1.test.ts b/packages/ai/src/ui/da-repro-1-unit-1.test.ts\n"
             "new file mode 100644\n--- /dev/null\n+++ b/packages/ai/src/ui/da-repro-1-unit-1.test.ts\n@@ -0,0 +1,1 @@\n+it('x')\n")
    d = _data(interrupt={"sha256": "abc123def4567890", "pr_body_path": "/x/PR.md"}, pr_patch=patch,
              commit_message="fix(ai): continue the retained text part on resume\n\nWhy.\n\nFixes #1\n")
    live = viewer.render(d, mode="live", token="tok")
    sheet = _sheet(live, "check-pr")
    # GitHub's words: title and number, Draft, "wants to merge 1 commit into main from …", its four tabs
    assert '<span id="pr-title">fix(ai): continue the retained text part on resume</span> <span class="gh-muted">#1</span>' in sheet
    assert 'gh-state draft">Draft</span><b>debugassist</b> wants to merge 1 commit into <code class="gh-ref">main</code>' in sheet
    assert [re.sub(r"<[^>]+>|\d", "", t).strip() for t in re.findall(r'role="tab"[^>]*>(.*?)</button>', sheet)] == \
        ["Conversation", "Commits", "Checks", "Files changed"]
    assert "Files changed <span class=\"cnt\">2</span>" in sheet
    # the commit, GitHub's two boxes: the message and the extended description, yours to edit
    assert 'id="commit-title" class="gh-input" value="fix(ai): continue the retained text part on resume"' in sheet
    assert 'id="commit-body"' in sheet and ">Why.\n\nFixes #1</textarea>" in sheet and "Restore the recommendation" in sheet
    assert 'class="ddel"' in sheet and 'class="dadd"' in sheet and "+2</span>" in sheet and "−1</span>" in sheet
    assert "abc123def456" in sheet and 'data-sha="abc123def4567890"' in sheet and "exactly this text and change" in sheet
    decide = sheet.split('class="decide gh-merge"')[1]
    assert "<span>Approve</span>" in decide and "<span>Close pull request</span>" in decide and "No checks ran" in decide
    assert "icon" not in decide and "<svg" not in decide and "✓" not in sheet
    assert 'fetch("/api/decide"' in live and "commit_message:" in live and live.count("fetch(") == 4
    saved = viewer.render(d)
    assert "fetch(" not in saved and "data-decide" not in saved and "uv run debug-assist approve ai-1-x" in saved


def test_a_refused_try_says_it_never_ran_and_the_command_shows_without_its_setup(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from debug_assist import testwriter
    monkeypatch.setattr(viewer, "_runs_dir", lambda: tmp_path)
    rd = tmp_path / "ai-1-x"
    (rd / "checkout").mkdir(parents=True)
    unit = "packages/ai/src/ui/da-repro-1-unit-1.test.ts"
    testwriter.write_proof(rd / "proof", unit, "t", "export COREPACK_HOME=/work/.corepack CI=1 && mkdir -p /work/.bin && "
                           "cd packages/ai && pnpm test:node src/ui/da-repro-1-unit-1.test.ts", 1, "AssertionError", "RED", "x",
                           SimpleNamespace(repo="vercel/ai", base_commit="e7f55a4", image="node"), rd / "checkout")
    st = {**_data()["state"], "repro": {"status": "REPRODUCED", "failing_test": unit, "evidence": "AssertionError: x",
                                        "ladder_plan": {"skipped": {"integration": "no recorded data beside packages/ai/src/ui"}}},
          "attempts": [{"step": "reproduce", "n": 1, "rung": "unit", "outcome": "RED", "test_path": unit, "evidence": "AssertionError: x"},
                       {"step": "reproduce", "n": 2, "rung": "unit", "outcome": "ERROR", "test_path": "",
                        "evidence": "writer refused: the test asserts nothing (no expect)"}]}
    sheet = _sheet(viewer.render(_data(state=st), mode="live"), "proof")
    assert "Not run: the draft was refused before it ran" in sheet and "the test asserts nothing (no expect)" in sheet
    assert "<summary>Run cd packages/ai &amp;&amp; pnpm test:node src/ui/da-repro-1-unit-1.test.ts</summary>" in sheet
    assert "Set up job" in sheet and "$ export COREPACK_HOME=/work/.corepack CI=1" in sheet          # folded, not gone
    assert "Not possible here" in sheet and "no recorded real data beside the code at fault (packages/ai/src/ui)" in sheet


def test_diffs_read_like_git():
    from debug_assist import diffview
    patch = ("diff --git a/a.ts b/a.ts\nindex 1..2 100644\n--- a/a.ts\n+++ b/a.ts\n@@ -5,4 +5,5 @@ fn\n same\n-gone <b>\n+came\n+also\n same2\n"
             + diffview.new_file_diff("t.test.ts", "line1\nline2"))
    files = diffview.parse(patch)
    assert [(f["path"], f["new"], f["added"], f["removed"]) for f in files] == [("a.ts", False, 2, 1), ("t.test.ts", True, 2, 0)]
    rows = files[0]["hunks"][0]["rows"]
    assert rows == [("ctx", 5, 5, "same"), ("del", 6, "", "gone <b>"), ("add", "", 6, "came"), ("add", "", 7, "also"), ("ctx", 7, 8, "same2")]
    h = diffview.html(patch)
    assert "2 files changed" in h and "&lt;b&gt;" in h and "<b>" not in h.replace("<b>", "", 0).split("gone")[1][:5]
    assert diffview.kind_of_test("packages/x/da-repro-1-integration-3.test.ts") == "integration" and diffview.kind_of_test("x.test.ts") == "unit"


def _rich_state():
    pack = {"issue": {"number": 7, "comments": [{"at": "2026-10-05T10:00:00Z", "text": "Still happens on **7.0.1**"}],
                      "linked": [{"repo": "vercel/ai", "number": 9, "title": "Fix resume", "state": "open", "pull_request": True, "merged": False}],
                      "errors": {"errors": ["TypeError: x is undefined"], "frames": []}},
            "code": {"best": "packages/ai/src/ui/chat.ts",
                     "ranking": [{"path": "packages/ai/src/ui/chat.ts", "score": 9, "matched": ["resumeStream"]},
                                 {"path": "packages/ai/src/ui/x.ts", "score": 2, "matched": []}],
                     "ctx": {"source": "packages/ai/src/ui/chat.ts", "snippets": "  881  const a = 1;\n  883  this.resumeStream();\n   …",
                             "look_in": ["packages/ai/src/ui/chat.ts"], "look_in_missing": ["packages/ai/src/nope.ts"],
                             "test_into": "packages/ai/src/ui/chat.test.ts"}},
            "related": [{"name": "processUIMessageStream", "module": "ai", "definition": "--- a.ts\n  1  export function p() {}", "used_in": 6}],
            "history": [{"path": "packages/ai/src/ui/chat.ts", "changes": [{"sha": "abc1234567", "date": "2026-09-30", "title": "feat: resume"}]}],
            "counts": {"comments": 1, "files": 2, "related": 1, "changes": 1}}
    story = ("### What broke?\nText parts were doubled after a resume.\n### Why didn't the tests catch it?\nNo test resumed twice.\n"
             "CONDITION: resume reused retained state")
    st = {**_data()["state"], "issue_url": "https://github.com/vercel/ai/issues/7", "context": {"sha256": "44b296c220e8aa"},
          "profile": {"repo": "vercel/ai", "base_commit": "e7f55a4"},
          "second_story": {"text": story},
          "guard": {"status": "CATCHES THE BUG", "covers": "every way a resumed stream can repeat a part",
                    "repo_path": "packages/ai/src/ui/da-guard-7.test.ts", "siblings": ["packages/react/src/x.ts:12"],
                    "on_fixed": {"passed": ["resume after text-start", "resume after text-delta"], "failed": ["resume twice"], "broken": []}}}
    return pack, st


def test_the_steps_open_their_own_sheets_github_style():
    """Isha 2026-10-09: Open issue on Read the issue; Context info (comments as comments, a commit list, code with the
    issue's strings marked); Read report (folded, GitHub-style) instead of the report on the page; Guard in plain words;
    tokens and cost on each AI step; metrics at the top; the problem section gone."""
    pack, st = _rich_state()
    calls = [{"step": "why_it_shipped", "input_tokens": 9500, "output_tokens": 3500, "actual_micro": 107700, "model": "anthropic/claude-opus-5.5", "at": "2026-10-08T21:13:20"},
             {"step": "why_it_shipped", "input_tokens": 500, "output_tokens": 500, "actual_micro": 2300, "model": "anthropic/claude-opus-5.5", "at": "2026-10-08T21:13:56"}]
    page = viewer.render(_data(state=st, pack=pack, calls=calls), mode="live", token="t")
    assert 'href="https://github.com/vercel/ai/issues/7" target="_blank" rel="noopener noreferrer"><span>Open issue</span>' in page
    for target, label in (("ctxinfo", "Context info"), ("report", "Read report"), ("guardinfo", "Guard")):
        assert f'popovertarget="{target}"><span>{label}</span>' in page and f'<div id="{target}" popover' in page, target
    assert page.index('class="metrics"') < page.index("<h2>Steps</h2>")                   # metrics beside the steps, at the top
    # Isha 2026-10-09: no Cost details; Last event beside the issue, not a tile; tokens inside each sheet, not on the rows
    assert "Cost details" not in page and 'id="costs"' not in page and "tokchip" not in page
    assert "Issue #1 · Standard AI · Last event" in page and "<span>Last event</span>" not in page
    assert ('<span class="Label">AI usage</span>14.0k tokens · $0.11 · 2 requests <span class="gh-muted">· 10,000 input, 4,000 output</span>'
            in _sheet(page, "report"))
    assert "The problem it is fixing" not in page and "<h2>What it read</h2>" not in page and 'id="story"' not in page
    ctx = _sheet(page, "ctxinfo")
    assert '<span class="Label">No AI</span><b>debugassist</b> read 1 comment, 2 files' in ctx and 'data-md="Still happens on **7.0.1**"' in ctx and "Comment 1" in ctx
    assert "Commits on <code>packages/ai/src/ui/chat.ts</code>" in ctx and "feat: resume" in ctx
    # Isha 2026-10-09: each commit opens on GitHub; each file opens on GitHub at the pinned version
    assert 'href="https://github.com/vercel/ai/commit/abc1234567" target="_blank"' in ctx and ">abc1234</a>" in ctx
    assert 'href="https://github.com/vercel/ai/blob/e7f55a4/packages/ai/src/ui/chat.ts"' in ctx and "View on GitHub" in ctx
    assert '<tr class="hit"><td class="ln">883</td>' in ctx and '<tr class=""><td class="ln">881</td>' in ctx
    # a listed file with no lines kept is a plain row, not one that looks like it opens (Isha 2026-10-09)
    assert '<div class="dfile"><div class="dfile-head"><span class="dpath">packages/ai/src/ui/x.ts</span>' in ctx
    assert "Your directional input" in ctx and "Not found in the code: packages/ai/src/nope.ts" in ctx and "processUIMessageStream" in ctx
    rep = _sheet(page, "report")
    assert '<span class="Label Label--done">AI-generated</span>' in rep and '<details class="gh-fold" open><summary>What broke?</summary>' in rep
    assert "<summary>Why didn&#x27;t the tests catch it?</summary>" in rep and "resume reused retained state" in rep
    g = _sheet(page, "guardinfo")
    assert "Test cases on the fixed code" in g and "2 passing · 1 failing · 0 errors" in g and ">Failing<" in g
    assert "still open: the fix does not cover it" in g and "AI-generated" in g
    assert "not changed by this fix" in g and "kept with the run, not added to the pull request" in g
    from debug_assist import plain
    assert plain.result("lasting_guard", st) == "A 3-case test for bugs like this one: 2 covered by the fix, 1 still open; the same code is in 1 other files"
    hosted = {"backtest": {"state": "NO PAST SIBLING FOUND", "candidates": [],
                           "detail": {"why": "the back-test runs only with the local sandbox for now"}}}
    assert plain.result("test_past_bugs", hosted) == ("Saved how this bug slipped through, for finding similar bugs. "
                                                      "Older versions were not checked: that works on the Mac only for now")
    # review of run #22543: on the Mac, the real reason, not "Mac only"
    no_anchor = {"backtest": {"state": "NO PAST SIBLING FOUND", "candidates": [], "detail": {
        "why": "Why it slipped found no commit that wrote the line, so there is no older version to start from"}}}
    assert plain.result("test_past_bugs", no_anchor).endswith("not checked: Why it slipped found no commit that wrote "
                                                              "the line, so there is no older version to start from")


def test_a_stopped_run_can_run_again_with_the_same_choices():
    st = {**_data()["state"], "issue_url": "https://github.com/vercel/ai/issues/7", "demo": True, "focus_heading": "Repro",
          "hints": {"look_in": ["a.ts"], "test_in": "a.test.ts"}, "outcome": {"exit": "NEVER REPRODUCED", "why": "w"}}
    page = viewer.render(_data(state=st, interrupt={}, next=[]), mode="live", token="t")
    m = re.search(r'data-again="([^"]+)"', page)
    import html as _h
    assert m and json.loads(_h.unescape(m.group(1))) == {"url": "https://github.com/vercel/ai/issues/7", "heading": "Repro",
                                                        "ai": "opus", "look_in": "a.ts", "test_in": "a.test.ts"}
    assert "Run again" in page and 'fetch("/api/start"' in page
    assert "data-again" not in viewer.render(_data(state=st, interrupt={}, next=[]))     # a saved page can't start runs


def test_checks_on_main_and_with_the_change():
    st = {"repro": {"existing_tests": {"status": "NONE FAIL", "package": "packages/ai", "passed": 4252, "failed": 0},
                    "failing_test": "p/da-repro-1-unit-1.test.ts", "ladder_plan": {"skipped": {"end_to_end": "x"}}},
          "fix": {"status": "VALIDATED", "holdout": {"test": "p/da-holdout-1.test.ts", "status": "PASSED"}}}
    got = [(c["name"], c["main"], c["change"]) for c in viewer.checks_of(st)]
    assert got == [("Existing tests / packages/ai", "pass", "pass"), ("Unit test / da-repro-1-unit-1.test.ts", "fail", "pass"),
                   ("Second test / da-holdout-1.test.ts", "fail", "pass"), ("Automation test (end to end)", "skip", "skip")]


def test_the_pull_request_opens_from_your_ok_row_not_spread_on_the_page():
    """Isha 2026-10-08: the pull request text as a sheet that opens from Your OK, as Check proof opens from Show the
    bug; not the full text on the page. Once decided, the same sheet reads Approved or Closed, with no buttons."""
    page = viewer.render(_data(), mode="live", token="t")
    rows = page.split("<h2>Steps</h2>")[1].split("</section>")[0]
    your_ok = [r for r in rows.split('<li class="row') if "<b>Your OK</b>" in r][0]
    assert 'popovertarget="check-pr"><span>Check PR</span>' in your_ok and "approve it or close it" in your_ok
    assert "The pull request text" not in page and 'id="pr"' not in page and '<div id="check-pr" popover' in page
    for status, word in (("APPROVED", "Approved"), ("REJECTED", "Closed")):
        st = {**_data()["state"], "approval": {"status": status}, "outcome": {"exit": "READY FOR YOU TO PUBLISH" if status == "APPROVED" else "REJECTED"}}
        done = viewer.render(_data(state=st, interrupt={}, next=[]), mode="live", token="t")
        sheet = _sheet(done, "check-pr")
        assert f'gh-state {"open" if word == "Approved" else "closed"}">{word}</span>' in sheet and "data-decide" not in sheet
        assert "Nothing was posted to GitHub." in sheet



def test_code_change_shows_the_code_at_fault_and_the_fix_git_style(tmp_path, monkeypatch):
    """Isha 2026-10-09 (F32 part A, view only): Find the cause opens the file at fault with its lines highlighted, why
    they are the cause and the plan; then the fix as a diff once Fix it has written it."""
    import subprocess
    monkeypatch.setattr(viewer, "_runs_dir", lambda: tmp_path)
    co = tmp_path / "ai-1-x" / "checkout"
    (co / "src").mkdir(parents=True)
    (co / "src" / "iter.ts").write_text("".join(f"line {n}\n" for n in range(1, 61)))
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b"]):
        subprocess.run(cmd, cwd=co, check=True)
    (co / "src" / "iter.ts").write_text("changed by the fix\n")       # the fix applied: the sheet still shows main
    st = {**_data()["state"], "cause": {"status": "FOUND", "file": "src/iter.ts", "lines": [30, 33], "why": "The **length** arm drops results.",
                                        "plan": "Pair the result with its call", "looked_up": ["addToolResultsToConversation"]}}
    patch = "diff --git a/src/iter.ts b/src/iter.ts\n--- a/src/iter.ts\n+++ b/src/iter.ts\n@@ -30,1 +30,1 @@\n-line 30\n+line 30 fixed\n"
    page = viewer.render(_data(state=st, patch=patch), mode="live", token="t")
    assert 'popovertarget="codechange"><span>Code change</span>' in page
    sh = _sheet(page, "codechange")
    assert "found the cause in <code class=\"gh-ref\">iter.ts</code>, lines 30–33" in sh and 'data-md="The **length** arm drops results."' in sh
    assert '<tr class="hit"><td class="ln">30</td><td class="code">line 30</td>' in sh and '<tr class=""><td class="ln">18</td>' in sh
    assert "Pair the result with its call" in sh and "addToolResultsToConversation" in sh
    assert 'class="ddel"' in sh and "line 30 fixed" in sh and "Files changed <span class=cnt>1</span>" in sh
    none = viewer.render(_data(state={**st, "fix": {}}, patch=""), mode="live", token="t")
    assert "No change yet: Fix it writes it next." in _sheet(none, "codechange")


def test_the_guard_says_which_cases_pass_on_the_old_code_on_purpose_and_what_it_checked():
    """Review of run #22543 (F9): 2 of 10 cases (controls) pass on the old code; the page said every case failed, and
    the words the symptom check used were dropped from the state."""
    g = {"on_fixed": {"passed": [f"case {i}" for i in range(10)], "failed": [], "broken": []}, "covers": "headers in each form",
         "checked_against": ["headers", "authorization"], "symptom_checked": True, "unfixed_passed": ["plain object", "empty Headers"]}
    sheet = viewer.guard_sheet({"guard": g})
    assert "8 of 10 cases failed on the old code" in sheet and "the other 2 pass there too, on purpose" in sheet
    assert "<code>headers</code>, <code>authorization</code>" in sheet
