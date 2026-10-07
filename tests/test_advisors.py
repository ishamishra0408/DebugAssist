"""The advisor seam: off unless an address AND a review are both set; advice never changes a run."""
import dataclasses
import json

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


def test_until_the_call_is_written_a_reviewed_server_is_skipped_not_crashed(monkeypatch, tmp_path):
    _cfg(monkeypatch, tmp_path, mcp="uv run advisors-mcp", reviewed=True)
    assert advisors.review({}, "lasting_guard", "guard")["status"] == "FAILED"
    assert preflight.check_advisors().status == "WARN"


def test_the_chart_shows_each_seat_beside_the_step_it_reviews(monkeypatch, tmp_path):
    _cfg(monkeypatch, tmp_path)
    svg = chart.svg(chart.advisor_states(None))
    assert 'id="ca-why"' in svg and "Advisor allspaw: off" in svg and "Advisor qe-ic-advisor: off" in svg
    done = chart.advisor_states({"state": {"advisors": {"why_it_shipped": {"status": "ANSWERED"}}}})
    assert done == {"why_it_shipped": "ANSWERED", "lasting_guard": "OFF"}
