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
    assert "30 min ago" in page and "ai-1-x-refix: 3 events" in page and "never counted in the north stars" in page
    assert "NOT SCORED" in page


def test_the_pipeline_card_marks_the_step_we_are_on_in_plain_words():
    st = {**_data()["state"], "triage": {"x": 1}, "cause": {"file": "f"}}
    live = viewer.render(_data(state=st, interrupt={}, next=["write_fix"], since="2026-10-07T06:49:28+00:00"), mode="live")
    assert 'class="pl live"' in live and "Step 4 of 9: Fix it · 32 s" in live and "Working on step 4 of 9: Fix it" in live
    assert live.count('class="node running"') == 1 and 'class="edge flow"' in live and 'class="dots"' in live
    assert "DebugAssistAgent" in live and "Nothing. Watch the steps." in live
    paused = viewer.render(_data(), mode="live")
    assert 'class="pl waiting"' in paused and "Waiting for your OK" in paused and 'class="node waiting"' in paused
    assert "uv run debug-assist approve ai-1-x" in paused and "uv run debug-assist reject ai-1-x" in paused
    assert 'class="edge flow"' not in paused  # nothing is moving while it waits for you
    stopped = viewer.render(_data(state={**st, "outcome": {"exit": "FIX NOT VALIDATED", "why": "w"},
                                         "log": ["write_fix: STOPPED FIX NOT VALIDATED"]}, next=[], interrupt={}), mode="live")
    assert 'class="pl stopped"' in stopped and "Stopped at step 4 of 9: Fix it" in stopped
    assert "None of its fixes passed the tests." in stopped and "There is nothing to approve." in stopped


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
    assert 'http-equiv="refresh"' not in served and "fetch(u" in served and 'data-k="now"' in served
    assert 'data-final="0"' in served
    still = viewer.render(_data(interrupt={}, next=["write_fix"]))
    assert "fetch(" not in still and 'http-equiv="refresh"' in still
    rp = viewer.render(_data(replay=True), mode="replay", replay={"speed": 8, "elapsed": 3, "length": 40, "final": False})
    assert "Replay of a past run" in rp and "3 of 40 s" in rp and "This is a recording of a past run." in rp


def test_a_run_that_has_not_started_yet_renders_as_getting_ready():
    page = viewer.render(_data(state={}, next=[], interrupt={}, events=[], pr_text="", patch=""), mode="live")
    assert "Getting ready" in page and "Nothing has happened yet" in page and 'data-final="0"' in page
    failed = viewer.render(_data(state={}, next=[], interrupt={}, events=[], pr_text="", patch="",
                                 console="PREFLIGHT FAIL   Docker          daemon not running\n"), mode="live")
    assert "Could not start" in failed and "Docker          daemon not running" in failed and 'data-final="1"' in failed


def test_a_stopped_run_says_no_fix_and_small_spend_shows():
    st = {**_data()["state"], "fix_clock": {"started_at": "x"}, "outcome": {"exit": "NEVER REPRODUCED", "why": "w"}}
    page = viewer.render(_data(state=st, next=[], interrupt={}, meter={"spent_usd": 0.00431, "cap_usd": 0.5}), mode="live")
    assert ">no fix<" in page and "$0.0043 of $0.50 limit" in page
    assert "It could not make the bug happen, so it did not try to fix it." in page


def test_a_run_that_went_quiet_reads_interrupted_with_the_command_to_continue():
    st = {**_data()["state"], "triage": {"x": 1}, "cause": {"file": "f"}}
    page = viewer.render(_data(state=st, interrupt={}, next=["write_fix"], since="2026-10-07T05:00:00+00:00",
                               now="2026-10-07T06:50:00+00:00"), mode="live")
    assert "Interrupted during step 4 of 9: Fix it" in page and "uv run debug-assist resume ai-1-x" in page
