"""The run viewer: read-only, escapes everything from outside, refreshes only while a run is live."""
from debug_assist import viewer


def _data(**over):
    d = {"run_id": "ai-1-x", "next": ["approval"], "interrupt": {"sha256": "abc123def456", "pr_body_path": "/x/PR.md"},
         "pr_text": "## Fix\n<script>alert(1)</script>", "pr_matches": True, "patch": "--- a/x\n+++ b/x\n-old\n+new",
         "events": [{"at": "2026-10-07T06:20:00+00:00", "step": "reproduce", "kind": "attempt", "rung": "unit", "n": 1, "outcome": "RED"}],
         "meter": {"spent_usd": 0.69, "cap_usd": 2.5, "sandbox_used_s": 49, "sandbox_cap_s": 1800}, "calls": [], "built": "now",
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
