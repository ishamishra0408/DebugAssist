"""Advisors: where DebugAssistAgent asks Devansh's advisor seats for a second opinion. Switched OFF until the Advisors
MCP server has been reviewed (standing rule: review it before anything connects). The rule is enforced here, not in
prose: it is on only when BOTH are set in .env, by Isha:

  ADVISORS_MCP        how to reach the server: a command (e.g. "uv run advisors-mcp") or an http(s) URL
  ADVISORS_REVIEWED   "yes", set only after the server's code and tool list were reviewed

An address without the review is refused at preflight. While off, a review returns OFF at once: no network, no
process, no cost, and no receipt (nothing was asked).

Where seats are asked. Advice only: an answer never changes a run's outcome, its PR text or the approval.
  why_it_shipped   allspaw         is the "why it slipped" report about conditions, never people?
  lasting_guard    qe-ic-advisor   is the check a condition (it runs by itself) or an instruction (someone must remember)?

Every real answer is an event in the run's log and a 5-field receipt appended to the bundle's usage_log.jsonl.
The call itself (_call) is written after the review, against the server's real tool names; until then it refuses.
"""
import json
from datetime import datetime

from . import events
from .config import CFG

REVIEWS = {
    "why_it_shipped": {"seat": "allspaw", "what": "the report on why the bug slipped through",
                       "question": "Is this second story about conditions, never people? Name any sentence that blames "
                                   "a person, and any juncture the evidence does not support."},
    "lasting_guard": {"seat": "qe-ic-advisor", "what": "the check for similar bugs",
                      "question": "Is this guard a condition (it runs by itself in CI) or an instruction (someone must "
                                  "remember it)? Which triggers of the bug's class does it miss?"},
}


CALL_WRITTEN = False   # True once _call is written against the reviewed server's tools


def status() -> tuple[str, str]:
    """(OFF | BLOCKED | ON, why) from the two settings."""
    if not CFG.advisors_mcp:
        return "OFF", "not connected"
    if not CFG.advisors_reviewed:
        return "BLOCKED", "an address is set, but the server has not been marked reviewed"
    return "ON", "connected"


def review(state: dict, step: str, evidence: str) -> dict:
    """Ask the step's seat about its output. Never raises; never changes the run."""
    r = REVIEWS[step]
    st, why = status()
    rec = {"seat": r["seat"], "step": step, "status": st, "what": r["what"]}
    if st == "ON":
        try:
            answer = _call(r["seat"], r["question"], evidence)
            rec.update(status="ANSWERED", answer=answer[:4000])
            receipt(r["seat"], f"{step}: {r['question']}", answer)
        except Exception as ex:  # an advisor that can't be reached costs the advice, never the run
            rec.update(status="FAILED", why=f"{type(ex).__name__}: {str(ex)[:200]}")
    else:
        rec["why"] = why
    events.log("advisor", key=f"{step}:{r['seat']}", **{k: v for k, v in rec.items() if k != "answer"},
               answered=rec.get("answer", "")[:300])
    return rec


def _call(seat: str, question: str, evidence: str) -> str:
    """Ask one seat over MCP. Written after the server is reviewed: its tool names and arguments are not known yet."""
    raise NotImplementedError("the Advisors MCP call is written after the server is reviewed (its tool names are "
                              "not known yet)")


def receipt(seat: str, question: str, answer: str) -> None:
    """One 5-field line appended to the bundle's usage_log.jsonl: the bundle's rule for every seat use."""
    line = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "seat": seat,
            "question": question[:300], "output_summary": " ".join(answer.split())[:300],
            "decision_changed": "none: advice only, recorded with the run"}
    with open(CFG.bundle_dir / "usage_log.jsonl", "a") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
