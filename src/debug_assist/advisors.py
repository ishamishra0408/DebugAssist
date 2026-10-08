"""Advisors: where DebugAssistAgent asks the Domain Expertise MCP server's advisor seats for a second opinion. Switched
OFF until the server has been reviewed (standing rule: review it before anything connects). The rule is enforced
here, not in prose: it is on only when BOTH are set where DebugAssistAgent runs, by Isha:

  ADVISORS_MCP        the server's MCP address, e.g. https://domain-expertise-mcp.onrender.com/mcp/
  ADVISORS_REVIEWED   "yes", set only after the server's code and tool list were reviewed (done 2026-10-08:
                      ~/Downloads/debug-assist-pipeline/design/advisors-mcp-review.md)
  ADVISORS_KEY        the server's API key (sent as a Bearer token; never logged, never in chat)

An address without the review is refused at preflight. While off, a review returns OFF at once: no network, no
process, no cost, and no receipt (nothing was asked).

Where seats are asked. Advice only: an answer never changes a run's outcome, its PR text or the approval.
  why_it_shipped   allspaw         is the "why it slipped" report about conditions, never people?
  lasting_guard    qe-ic-advisor   is the check a condition (it runs by itself) or an instruction (someone must remember)?

Every real answer is an event in the run's log and a 5-field receipt appended to the bundle's usage_log.jsonl.
The call (_call) is one MCP JSON-RPC `tools/call` of the server's `advise` tool, over Streamable HTTP. The server's
seats are deterministic checks, not a model: allspaw scans each sentence for blame wording; qe-ic-advisor says whether
a guard is a condition (runs by itself) or an instruction (someone must remember it). summarize() turns its
JudgmentResult into plain sentences. The answer is shown and logged only: it never reaches a prompt or a decision.
"""
import json
import os
import urllib.error
import urllib.request
from datetime import datetime

from . import events
from .config import CFG

REVIEWS = {
    "why_it_shipped": {"seat": "allspaw", "what": "the report on why the bug slipped through",
                       "question": "Does any sentence in the report blame a person? It checks every sentence for blame "
                                   "wording (\"should have\", \"forgot to\", \"be more careful\")."},
    "lasting_guard": {"seat": "qe-ic-advisor", "what": "the check for similar bugs",
                      "question": "Is the guard a condition that runs by itself (a test, a CI check, an assert) or an "
                                  "instruction someone must remember?"},
}

CALL_WRITTEN = True    # _call is written against the reviewed server's `advise` tool (2026-10-08)
TIMEOUT_S = 90         # the server is on a free plan that sleeps; the first call of a while wakes it (about 30 s)


class AdvisorError(RuntimeError):
    pass


def status() -> tuple[str, str]:
    """(OFF | BLOCKED | ON, why) from the two settings."""
    if not CFG.advisors_mcp:
        return "OFF", "not connected"
    if not CFG.advisors_reviewed:
        return "BLOCKED", "an address is set, but the server has not been marked reviewed"
    return "ON", "connected"


def review(state: dict, step: str, evidence: str, question: str = "") -> dict:
    """Ask the step's seat about its output. Never raises; never changes the run. question: what the server's template
    files beside the evidence (allspaw: the fix's summary; qe-ic-advisor: what the guard is)."""
    r = REVIEWS[step]
    st, why = status()
    rec = {"seat": r["seat"], "step": step, "status": st, "what": r["what"]}
    if st == "ON":
        try:
            answer = _call(r["seat"], question or r["question"], evidence)
            rec.update(status="ANSWERED", answer=answer[:4000])
        except Exception as ex:  # an advisor that can't be reached costs the advice, never the run
            rec.update(status="FAILED", why=f"{type(ex).__name__}: {str(ex)[:200]}")
        if rec["status"] == "ANSWERED":
            try:
                receipt(r["seat"], f"{step}: {r['question']}", rec["answer"])
                rec["receipt"] = "written"
            except OSError:  # a host without the bundle (Render): the answer stands; the run's event log keeps it
                rec["receipt"] = "not written: no advisor bundle on this host"
    else:
        rec["why"] = why
    events.log("advisor", key=f"{step}:{r['seat']}", **{k: v for k, v in rec.items() if k != "answer"},
               answered=rec.get("answer", "")[:300])
    return rec


def _call(seat: str, question: str, evidence: str, consumer: str = "debugassist") -> str:
    """Ask one seat through the server's `advise` tool; the answer in plain sentences."""
    result = mcp_call("tools/call", {"name": "advise", "arguments": {
        "seat_alias": seat, "question": question[:4000], "evidence": evidence[:12000], "consumer": consumer}})
    return summarize(seat, result)


SAMPLES = {  # made-up, fixed: what `debug-assist advisors-check` asks (consumer "test", so the server can tell)
    "why_it_shipped": ("The fix: the stream's finalizer emits only tool calls whose input finished.",
                       "C1 — the finalizer could not tell a finished stream from a broken one. C2 — no test cut a "
                       "stream mid tool call. The reviewer should have caught it."),
    "lasting_guard": ("The guard is a test that runs by itself with the package's tests: the test fails whenever a "
                      "stream ends before a tool call is complete.",
                      "The bug: a tool call cut off mid-stream was reported as complete."),
}


def check_both() -> list[tuple[str, str, str]]:
    """(seat, status, what it said) for each review point, asked one fixed sample each: the wire, end to end."""
    out = []
    for step, r in REVIEWS.items():
        q, ev = SAMPLES[step]
        try:
            out.append((r["seat"], "ANSWERED", _call(r["seat"], q, ev, consumer="test")))
        except Exception as ex:
            out.append((r["seat"], "FAILED", f"{type(ex).__name__}: {str(ex)[:200]}"))
    return out


def mcp_call(method: str, params: dict, timeout: float = TIMEOUT_S) -> dict:
    """One MCP JSON-RPC request to ADVISORS_MCP over Streamable HTTP (the server is stateless: no session to open).
    Returns the tool's structured result (or tools/list's result); raises AdvisorError with a plain reason."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    key = os.environ.get("ADVISORS_KEY", "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(CFG.advisors_mcp, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, ctype = resp.read().decode("utf-8", "replace"), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise AdvisorError("the server refused the key (401): check ADVISORS_KEY") from None
        raise AdvisorError(f"the server answered HTTP {e.code}") from None
    msg = _rpc_message(raw, ctype)
    if "error" in msg:
        raise AdvisorError(f"the server said: {(msg['error'] or {}).get('message', 'error')}"[:300])
    res = msg.get("result") or {}
    if method != "tools/call":
        return res
    text = " ".join(c.get("text", "") for c in res.get("content") or [] if c.get("type") == "text")
    if res.get("isError"):
        raise AdvisorError(f"the tool said: {text[:300]}")
    if isinstance(res.get("structuredContent"), dict):
        return res["structuredContent"]
    try:
        return json.loads(text)
    except ValueError:
        raise AdvisorError("the answer was not the expected JSON") from None


def _rpc_message(raw: str, ctype: str) -> dict:
    """The JSON-RPC response with id 1, from a JSON body or a server-sent-events stream (`data: {...}` lines)."""
    if "text/event-stream" in ctype or raw.lstrip().startswith(("event:", "data:")):
        msgs = []
        for line in raw.splitlines():
            if line.startswith("data:"):
                try:
                    msgs.append(json.loads(line[5:].strip()))
                except ValueError:
                    continue
        return next((m for m in reversed(msgs) if m.get("id") == 1), msgs[-1] if msgs else {})
    try:
        return json.loads(raw)
    except ValueError:
        raise AdvisorError("the server's reply was not JSON") from None


def summarize(seat: str, result: dict) -> str:
    """The seat's JudgmentResult in plain sentences: what it found, and its reference for later."""
    mr = result.get("machine_result") or {}
    state = mr.get("state") or result.get("verdict") or ""
    lines = []
    if state == "DECLINED":
        lines.append(f"It declined ({mr.get('reason', 'no reason given')}): needs {', '.join(mr.get('needs') or []) or 'more input'}.")
    if seat == "allspaw":
        blame = mr.get("blame_sentences") or []
        lines.append("Blame check: no sentence blames a person." if not blame else
                     f"Blame check: {len(blame)} sentence{'s' * (len(blame) != 1)} read as blaming a person: "
                     + " | ".join(f'"{b[:200]}"' for b in blame[:5]))
    elif seat == "qe-ic-advisor":
        kind, matched = mr.get("guard_kind"), mr.get("guard_patterns_matched") or {}
        said = {"condition": "a condition: it runs by itself", "instruction": "an instruction: someone must remember it",
                "unclear": "unclear: it found none of the phrases it looks for"}.get(kind, kind or "not given")
        found = ", ".join(f'"{x}"' for x in (matched.get("condition") or []) + (matched.get("instruction") or []))
        lines.append(f"Guard check: {said}" + (f" (matched {found})." if found else "."))
        if state == "FAIL" and not mr.get("flip_condition"):
            lines.append("Its fix verdict reads FAIL only because this route sends it no verdict to check; it is not a "
                         "judgment of the fix.")
    else:
        lines.append(f"Verdict: {state or 'none'}.")
    if result.get("judgment_id"):
        lines.append(f"Reference {result['judgment_id']}.")
    return " ".join(lines)[:4000]


def receipt(seat: str, question: str, answer: str) -> None:
    """One 5-field line appended to the bundle's usage_log.jsonl: the bundle's rule for every seat use."""
    line = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "seat": seat,
            "question": question[:300], "output_summary": " ".join(answer.split())[:300],
            "decision_changed": "none: advice only, recorded with the run"}
    with open(CFG.bundle_dir / "usage_log.jsonl", "a") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
