"""The advisor seam: off unless an address AND a review are both set; advice never changes a run."""
import dataclasses
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from debug_assist import advisors, chart, preflight


def _cfg(monkeypatch, tmp_path, mcp="", reviewed=False):
    monkeypatch.setattr(advisors, "CFG", dataclasses.replace(advisors.CFG, advisors_mcp=mcp, advisors_reviewed=reviewed,
                                                              bundle_dir=tmp_path))


def test_off_by_default_asks_nothing_and_writes_no_receipt(monkeypatch, tmp_path):
    _cfg(monkeypatch, tmp_path)
    monkeypatch.setattr(advisors, "_call", lambda *a: (_ for _ in ()).throw(AssertionError("must not be called")))
    rec = advisors.review({}, "why_it_shipped", "story")
    assert rec["status"] == "OFF" and rec["seat"] == "allspaw" and not (tmp_path / "usage_log.jsonl").exists()
    assert preflight.check_advisors().status == "PASS"


def test_an_address_without_the_review_is_refused_at_the_door(monkeypatch, tmp_path):
    _cfg(monkeypatch, tmp_path, mcp="uv run advisors-mcp")
    assert advisors.review({}, "lasting_guard", "guard")["status"] == "BLOCKED"
    c = preflight.check_advisors()
    assert c.status == "FAIL" and "ADVISORS_REVIEWED=yes" in c.fix


def test_a_real_answer_is_logged_as_a_receipt_and_changes_nothing_else(monkeypatch, tmp_path):
    _cfg(monkeypatch, tmp_path, mcp="uv run advisors-mcp", reviewed=True)
    monkeypatch.setattr(advisors, "_call", lambda seat, q, ev: "No sentence blames a person.")
    rec = advisors.review({}, "why_it_shipped", "story")
    assert rec["status"] == "ANSWERED" and rec["answer"] == "No sentence blames a person."
    line = json.loads((tmp_path / "usage_log.jsonl").read_text())
    assert set(line) == {"ts", "seat", "question", "output_summary", "decision_changed"} and line["seat"] == "allspaw"
    assert line["decision_changed"].startswith("none")


# what the live server answered on 2026-10-08 (its open demo, made-up input), trimmed
ALLSPAW = {"seat": "allspaw-debugassist", "judgment_id": "judg_d0b404bac15e", "verdict": "READY_FOR_JUDGMENT",
           "machine_result": {"verdict": "READY_FOR_JUDGMENT", "state": "READY_FOR_JUDGMENT", "seat": "allspaw",
                              "blame_sentences": ["The reviewer should have caught it."]}}
TRIAGE = {"seat": "defect-triage", "judgment_id": "judg_t1", "verdict": "DEFECT",
          "machine_result": {"verdict": "DEFECT", "state": "DEFECT", "is_defect": 0.9, "confidence": 0.9}}
LOCATE = {"seat": "cause-locator", "judgment_id": "judg_l1", "verdict": "CANDIDATES",
          "machine_result": {"verdict": "CANDIDATES", "state": "CANDIDATES",
                             "candidates": [{"path": "packages/ai/src/tracker.ts", "lines": "40-60", "confidence": 0.5}]}}
QEIC = {"seat": "qe-ic-debugassist", "judgment_id": "judg_45d6b5d8dee8", "verdict": "FAIL",
        "machine_result": {"verdict": "FAIL", "state": "FAIL", "check": "unstated", "guard_kind": "condition",
                           "guard_patterns_matched": {"condition": ["test fails"], "instruction": []}}}


@pytest.fixture
def fake_server(monkeypatch, tmp_path):
    """A stand-in for the Domain Expertise MCP server: Streamable HTTP, answers as a server-sent event, needs the key."""
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"auth": self.headers.get("Authorization"), "accept": self.headers.get("Accept"), "body": body})
            if self.headers.get("Authorization") != "Bearer k3y":
                self.send_response(401)
                self.end_headers()
                return
            if body["method"] == "tools/list":
                result = {"tools": [{"name": "advise"}, {"name": "allspaw_debugassist"}]}
            else:
                if body["params"]["name"] == "defect_triage":
                    sc = TRIAGE
                else:
                    sc = {"allspaw": ALLSPAW, "cause-locator": LOCATE}.get(body["params"]["arguments"]["seat_alias"], QEIC)
                result = {"content": [{"type": "text", "text": json.dumps(sc)}], "structuredContent": sc, "isError": False}
            data = f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': result})}\n\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    httpd = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _cfg(monkeypatch, tmp_path, mcp=f"http://127.0.0.1:{httpd.server_address[1]}/mcp/", reviewed=True)
    monkeypatch.setenv("ADVISORS_KEY", "k3y")
    yield seen
    httpd.shutdown()


def test_both_review_points_ask_the_server_and_say_what_it_found_in_plain_words(fake_server, tmp_path):
    rec = advisors.review({}, "why_it_shipped", "C1 — no test cut a stream. The reviewer should have caught it.",
                          question="The fix: flush emits only finished tool calls")
    assert rec["status"] == "ANSWERED" and rec["receipt"] == "written" and rec["raw"]["judgment_id"] == "judg_d0b404bac15e"
    assert rec["answer"] == ('Blame check: 1 sentence read as blaming a person: "The reviewer should have caught it." '
                             "Reference judg_d0b404bac15e.")
    call = fake_server[0]
    assert call["auth"] == "Bearer k3y" and "text/event-stream" in call["accept"]
    assert call["body"]["method"] == "tools/call" and call["body"]["params"]["name"] == "advise"
    assert call["body"]["params"]["arguments"] == {"seat_alias": "allspaw", "question": "The fix: flush emits only finished tool calls",
                                                   "evidence": "C1 — no test cut a stream. The reviewer should have caught it.",
                                                   "consumer": "debugassist"}
    rec = advisors.review({}, "lasting_guard", "The bug: x", question="The guard is a test: the test fails whenever x.")
    assert rec["answer"].startswith('Guard check: a condition: it runs by itself (matched "test fails").')
    assert "reads FAIL only because this route sends it no verdict" in rec["answer"]       # never shown as a failed fix
    assert len((tmp_path / "usage_log.jsonl").read_text().splitlines()) == 2
    assert preflight.check_advisors().status == "PASS"


def test_a_wrong_key_or_a_missing_bundle_never_costs_the_run(fake_server, monkeypatch, tmp_path):
    monkeypatch.setenv("ADVISORS_KEY", "wrong")
    rec = advisors.review({}, "why_it_shipped", "story")
    assert rec["status"] == "FAILED" and "refused the key (401)" in rec["why"] and "wrong" not in json.dumps(rec)
    c = preflight.check_advisors()
    assert c.status == "WARN" and "refused the key" in c.fact and "optional" in c.fix
    monkeypatch.setenv("ADVISORS_KEY", "k3y")
    monkeypatch.setattr(advisors, "CFG", dataclasses.replace(advisors.CFG, bundle_dir=tmp_path / "no-such-bundle"))
    rec = advisors.review({}, "why_it_shipped", "story")
    assert rec["status"] == "ANSWERED" and rec["receipt"].startswith("not written")       # Render has no bundle


def test_an_unreachable_server_is_a_note_not_a_stop(monkeypatch, tmp_path):
    _cfg(monkeypatch, tmp_path, mcp="http://127.0.0.1:9/mcp/", reviewed=True)
    assert advisors.review({}, "lasting_guard", "guard")["status"] == "FAILED"
    assert preflight.check_advisors().status == "WARN"


def test_the_chart_shows_each_seat_beside_the_step_it_reviews(monkeypatch, tmp_path):
    _cfg(monkeypatch, tmp_path)
    svg = chart.svg(chart.advisor_states(None))
    assert 'id="ca-why"' in svg and "Advisor allspaw: off" in svg and "Advisor qe-ic-advisor: off" in svg
    done = chart.advisor_states({"state": {"advisors": {"why_it_shipped": {"status": "ANSWERED"}}}})
    assert done == {"read_issue": "OFF", "find_cause": "OFF", "why_it_shipped": "ANSWERED", "lasting_guard": "OFF"}
    assert 'id="ca-triaged"' in svg and 'id="ca-cause"' in svg and "Advisor cause-locator: off" in svg


def test_the_check_asks_both_review_points_once_and_says_it_is_a_test(fake_server):
    got = advisors.check_both()
    assert [g[:2] for g in got] == [("defect-triage", "ANSWERED"), ("cause-locator", "ANSWERED"),
                                    ("allspaw", "ANSWERED"), ("qe-ic-advisor", "ANSWERED")]
    assert {c["body"]["params"]["arguments"].get("consumer") for c in fake_server
            if c["body"]["params"]["name"] == "advise"} == {"test"}                          # not counted as a real run


def test_triage_with_numbers_uses_its_own_rule_and_the_locator_reads_suspects(fake_server):
    rec = advisors.review({}, "read_issue", "the issue body", question="the title", context={"repo_name": "vercel/ai"},
                          numbers={"is_defect": 0.9, "confidence": 0.9})
    call = fake_server[-1]["body"]["params"]
    assert call["name"] == "defect_triage" and call["arguments"]["is_defect"] == 0.9 and call["arguments"]["repo"] == {"name": "vercel/ai"}
    assert rec["answer"].startswith("Verdict: a real defect (likelihood 0.9, sure 0.9).")
    rec = advisors.review({}, "find_cause", "what went wrong", question="Where is the cause?", context={
        "repo_listing": ["packages/ai/src/tracker.ts"], "repro_output": "AssertionError", "candidates": []})
    call = fake_server[-1]["body"]["params"]
    assert call["name"] == "advise" and call["arguments"]["seat_alias"] == "cause-locator" and "repo_listing" in call["arguments"]["context"]
    assert rec["answer"].startswith("Kept 1 suspect: packages/ai/src/tracker.ts lines 40-60 (confidence 0.5).")
    assert advisors.summarize("defect-triage", {"machine_result": {"state": "NEEDS_PERSON", "question": "Which version?",
                                                                   "answerer": "the reporter"}}).startswith(
        "Verdict: a person decides. The missing fact to ask the reporter: Which version?")
    assert advisors.summarize("cause-locator", {"machine_result": {"state": "CAUSE_NOT_FOUND", "code": "NO_LOCATING_CUE"}}) == \
        "Not located: no failing output to locate from."
