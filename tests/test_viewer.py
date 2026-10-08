"""The run viewer: read-only, escapes everything from outside, refreshes only while a run is live."""
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
    assert "uv run debug-assist approve ai-1-x" in paused and "uv run debug-assist reject ai-1-x" in paused
    assert "Approve in your terminal" in paused and "Nothing is posted to GitHub." in paused
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
    assert 'popovertarget="acts"' in page and '<div id="acts" popover class="pop glass"' in page
    body = page.split("<main>")[1].split("</main>")[0]
    assert "Latest activity" not in body and 'data-k="log"' not in body           # not a section on the page any more
    assert re.search(r'class="badge"[^>]*>3<', page)
    for word in ("Usage and cost", "Time to fix", "LLM cost", "Tokens", "4.5k in · 600 out", "3 model calls", "Compute time",
                 "Last event"):
        assert word in page, word
    assert "Time and money" not in page and "Money spent" not in page


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
    assert 'popovertarget="proof"' in page and "See the proof" in page and '<div id="proof" popover' in page
    sheet = page.split('<div id="proof"')[1].split('<div id="acts"')[0]
    text = re.sub(r"<[^>]+>", " ", sheet)
    # the checklist, in Isha's order: the repo's own tests first, then what was written at each level
    qs = re.findall(r'class="qt">([^<]+)<', sheet)
    assert qs == ["Test already in the repo failing for this issue?", "Unit test failing for this issue?",
                  "Integration test failing for this issue?", "Automation test (end to end) failing for this issue?"]
    assert "All 412 of the repo&#x27;s own tests in packages/p pass on the unfixed code" in sheet
    assert re.search(r"Unit test failing.*?Found.*?Written for this issue \(try 2\).*?Passes after the fix", sheet, re.S)
    assert re.search(r"Integration test failing.*?Found.*?on real recorded data.*?Passes after the fix", sheet, re.S)
    assert re.search(r"Automation test.*?Not tried.*?live API keys", sheet, re.S)
    # every try is a page, turned by number; the pager opens on the try that showed the bug
    assert sheet.count('class="page proof-card"') == 3 and re.findall(r'data-go="(\d)"', sheet) == ["0", "1", "2"]
    assert 'data-start="1"' in sheet and "Failed, but for another reason" in text and text.count("Shows the bug") == 2
    assert "What the test checks:</b> emits a tool-call" in sheet and "it(&#x27;shows&#x27;" in sheet and "it(&#x27;confirms&#x27;" in sheet
    assert "<code>pnpm test:node x</code>" in sheet and "Docker container from node:22, internet off" in sheet
    assert plain.result("reproduce", st) == "Bug shown on try 2, then confirmed on a recorded stream (try 3)"
    old = {**st, "repro": {**st["repro"], "existing_tests": None}, "fix": {}}
    sheet = viewer.render(_data(state=old), mode="live").split('<div id="proof"')[1]
    assert "Not checked" in sheet and "Waiting for the fix" in sheet
    no = viewer.render(_data(state={**st, "repro": {"status": "NEVER REPRODUCED"}}), mode="live")
    assert "See the proof" not in no
