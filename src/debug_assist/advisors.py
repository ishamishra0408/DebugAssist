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
  read_issue       defect-triage   a real defect? its fixed rule over the triage's numbers (sure enough? then by 0.5)
  find_cause       cause-locator   do the suspects hold up? (at most 3, real files, a failing output to locate from)
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

REVIEWS = {  # in the order of the steps
    "read_issue": {"seat": "defect-triage", "what": "whether the issue is a real defect",
                   "question": "Is this a real defect? A fixed rule over the triage's numbers: not sure enough (under "
                               "0.6) and a person decides, with the one missing fact named; otherwise defect or not by 0.5."},
    "find_cause": {"seat": "cause-locator", "what": "where the cause is",
                   "question": "Do the suspects hold up? At most three, only files that exist in the repo, a failing "
                               "output to locate from, and nothing below its confidence bar."},
    "why_it_shipped": {"seat": "allspaw", "what": "the report on why the bug slipped through",
                       "question": "Does any sentence in the report blame a person? It checks every sentence for blame "
                                   "wording (\"should have\", \"forgot to\", \"be more careful\")."},
    "lasting_guard": {"seat": "qe-ic-advisor", "what": "the check for similar bugs",
                      "question": "Is the guard a condition that runs by itself (a test, a CI check, an assert) or an "
                                  "instruction someone must remember?"},
}

# Asking a seat by hand (the Connect page): its boxes, as the server reads them (name, label, hint, kind).
# role and motto: the advisors' own roster (domain-expertise-mcp seats.json, 21a85cd); palette: their scene's colours
ASK = {
    "defect-triage": {"gets": "The issue's title and text, the repo, and the triage's numbers: how likely a defect, how sure.",
                     "role": "Triage", "motto": "Real defect or not?", "palette": "tide",
                      "useful": "By hand it has no triage numbers, so it names the one missing fact that would settle it.",
                      "fields": [("title", "Issue title", "e.g. Streaming drops a tool call's input", "input"),
                                 ("body", "Issue text", "Paste the issue as the reporter wrote it", "textarea"),
                                 ("repo", "Repository", "owner/name, e.g. vercel/ai", "input")]},
    "cause-locator": {"gets": "The cause step's suspects (at most 3), the files of their packages, and the failing test's output.",
                     "role": "Localization", "motto": "Where's the cause?", "palette": "violet",
                      "useful": "By hand, the files you list are both its suspects and its file list: it keeps the ones "
                                "that hold up, at most three.",
                      "fields": [("desc", "What went wrong", "One or two lines", "textarea"),
                                 ("repro", "Error or failing test output", "Paste the stack trace or the failed assertion", "textarea"),
                                 ("files", "Suspect files, one per line", "e.g. packages/ai/src/stream.ts", "textarea")]},
    "allspaw": {"gets": "The fix's summary and the report on why the bug slipped through.",
               "role": "Incident review", "motto": "Conditions, not culprits.", "palette": "ember",
                "useful": "Paste any report on why a bug slipped through: it flags every sentence that blames a person.",
                "fields": [("q", "The fix, in one line", "e.g. The finalizer now emits only tool calls whose input finished", "input"),
                           ("e", "Text to check for blame", "Paste a report on why a bug slipped through", "textarea")]},
    "qe-ic-advisor": {"gets": "What the guard is, and the bug it guards against.",
                     "role": "Quality gate", "motto": "Ship or stop.", "palette": "moss",
                      "useful": "Describe any guard: it says whether it runs by itself or needs someone to remember it.",
                      "fields": [("q", "What the guard is", "e.g. A test that fails whenever a stream ends early", "input"),
                                 ("e", "The bug", "One or two lines: what went wrong", "textarea")]},
}

CALL_WRITTEN = True    # _call is written against the reviewed server's `advise` tool (2026-10-08)
TIMEOUT_S = 90         # the server is on a free plan that sleeps; the first call of a while wakes it (about 30 s)


class AdvisorError(RuntimeError):
    pass


class Answer(str):
    """The plain-sentence summary, carrying the server's whole answer (.raw) so the page can show it in full."""
    raw: dict = {}


RAW_MAX = 16000   # characters of the server's answer kept with a run (its answers are a few KB)


def status() -> tuple[str, str]:
    """(OFF | BLOCKED | ON, why) from the two settings."""
    if not CFG.advisors_mcp:
        return "OFF", "not connected"
    if not CFG.advisors_reviewed:
        return "BLOCKED", "an address is set, but the server has not been marked reviewed"
    return "ON", "connected"


def review(state: dict, step: str, evidence: str, question: str = "", context: dict | None = None,
           numbers: dict | None = None) -> dict:
    """Ask the step's seat about its output. Never raises; never changes the run. question: what the server's template
    files beside the evidence (allspaw: the fix's summary; qe-ic-advisor: what the guard is; defect-triage: the issue's
    title). context: what some seats also read (repo_name; repo_listing, repro_output, candidates). numbers: the
    triage's is_defect and confidence, for defect-triage's own rule."""
    r = REVIEWS[step]
    st, why = status()
    rec = {"seat": r["seat"], "step": step, "status": st, "what": r["what"]}
    if st == "ON":
        try:
            extra = {k: v for k, v in (("context", context), ("numbers", numbers)) if v}
            answer = _call(r["seat"], question or r["question"], evidence, **extra)
            rec.update(status="ANSWERED", answer=str(answer)[:4000])
            if getattr(answer, "raw", None):
                rec["raw"] = answer.raw
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
    events.log("advisor", key=f"{step}:{r['seat']}", **{k: v for k, v in rec.items() if k not in ("answer", "raw")},
               answered=rec.get("answer", "")[:300])
    return rec


def _call(seat: str, question: str, evidence: str, consumer: str = "debugassist", context: dict | None = None,
          numbers: dict | None = None) -> str:
    """Ask one seat; the answer in plain sentences. Through the `advise` tool, except defect-triage with the triage's
    numbers: advise() carries no numbers, so it could only ever answer NEEDS_PERSON; its own tool applies its rule."""
    if seat == "defect-triage" and numbers:
        args = {"issue": {"title": question[:500], "body": evidence[:12000]},
                "repo": {"name": (context or {}).get("repo_name", "")},
                "is_defect": float(numbers["is_defect"]), "confidence": float(numbers["confidence"])}
        result = mcp_call("tools/call", {"name": "defect_triage", "arguments": args})
    else:
        args = {"seat_alias": seat, "question": question[:4000], "evidence": evidence[:12000], "consumer": consumer}
        if context:
            args["context"] = context
        result = mcp_call("tools/call", {"name": "advise", "arguments": args})
    return summarize(seat, result)


SAMPLES = {  # made-up, fixed: what `debug-assist advisors-check` asks (consumer "test", so the server can tell)
    "read_issue": {"question": "Streaming drops a tool call's input", "evidence": "When the stream is cut mid tool call, "
                   "the client receives a tool call with an empty input instead of an error.",
                   "context": {"repo_name": "vercel/ai"}, "numbers": {"is_defect": 0.9, "confidence": 0.9}},
    "find_cause": {"question": "Where is the cause?", "evidence": "A tool call cut off mid-stream is reported as complete.",
                   "context": {"repo_listing": ["packages/ai/src/stream.ts", "packages/ai/src/tracker.ts"],
                               "repro_output": "AssertionError: expected [ { type: 'tool-call' } ] to strictly equal []",
                               "candidates": [{"path": "packages/ai/src/tracker.ts", "lines": "40-60",
                                               "reason": "flush emits unfinished calls", "confidence": 0.5}]}},
    "why_it_shipped": {"question": "The fix: the stream's finalizer emits only tool calls whose input finished.",
                       "evidence": "C1 — the finalizer could not tell a finished stream from a broken one. C2 — no test "
                                   "cut a stream mid tool call. The reviewer should have caught it."},
    "lasting_guard": {"question": "The guard is a test that runs by itself with the package's tests: the test fails "
                                  "whenever a stream ends before a tool call is complete.",
                      "evidence": "The bug: a tool call cut off mid-stream was reported as complete."},
}


def ask(seat: str, fields: dict) -> str:
    """One question asked by hand (the Connect page), marked consumer "operator" so the server can tell it from runs.
    fields: the seat's boxes by name (ASK). Leaves a receipt like every seat use. Raises AdvisorError / ValueError."""
    if seat not in ASK:
        raise ValueError(f"no such advisor: {seat}")
    got = {name: str(fields.get(name, "")).strip() for name, *_ in ASK[seat]["fields"]}
    empty = [label for name, label, *_ in ASK[seat]["fields"] if not got[name]]
    if empty:
        raise ValueError("fill in every box: " + ", ".join(label.lower() for label in empty))
    if seat == "defect-triage":
        said = _call(seat, got["title"][:500], got["body"], consumer="operator", context={"repo_name": got["repo"][:200]})
    elif seat == "cause-locator":
        files = [f.strip() for f in got["files"].splitlines() if f.strip()][:200]
        said = _call(seat, "Where is the cause?", got["desc"], consumer="operator", context={
            "repo_listing": files, "repro_output": got["repro"][:6000],
            "candidates": [{"path": f, "reason": "named by hand", "confidence": 0.5} for f in files[:3]]})
    else:
        said = _call(seat, got["q"][:4000], got["e"][:12000], consumer="operator")
    try:
        receipt(seat, f"asked by hand: {next(iter(got.values()))[:200]}", said)
    except OSError:
        pass
    return said


def check_both() -> list[tuple[str, str, str]]:
    """(seat, status, what it said) for each review point, asked one fixed sample each: the wire, end to end."""
    out = []
    for step, r in REVIEWS.items():
        sample = SAMPLES[step]
        try:
            said = _call(r["seat"], sample["question"], sample["evidence"], consumer="test",
                         **{k: sample[k] for k in ("context", "numbers") if k in sample})
            out.append((r["seat"], "ANSWERED", said))
        except Exception as ex:
            out.append((r["seat"], "FAILED", f"{type(ex).__name__}: {str(ex)[:200]}"))
            continue
        try:  # every seat use leaves a receipt, test samples too (the bundle's rule)
            receipt(r["seat"], f"advisors-check (fixed test sample) for {step}: {r['question']}", said)
        except OSError:
            pass
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
    elif seat == "defect-triage":
        if state == "DEFECT":
            lines.append(f"Verdict: a real defect (likelihood {mr.get('is_defect')}, sure {mr.get('confidence')}).")
        elif state == "NOT_A_DEFECT":
            lines.append(f"Verdict: not a defect (likelihood {mr.get('is_defect')}, sure {mr.get('confidence')}).")
        elif state == "NEEDS_PERSON":
            who = mr.get("answerer") or "the reporter or a maintainer"
            lines.append(f"Verdict: a person decides. The missing fact to ask {who}: {mr.get('question', 'not given')}")
    elif seat == "cause-locator":
        if state == "CANDIDATES":
            kept = mr.get("candidates") or []
            lines.append(f"Kept {len(kept)} suspect{'s' * (len(kept) != 1)}: " + "; ".join(
                f"{c.get('path')}" + (f" lines {c['lines']}" if c.get("lines") else "") + f" (confidence {c.get('confidence')})"
                for c in kept) + ".")
        elif state == "CAUSE_NOT_FOUND":
            why = {"NO_LOCATING_CUE": "no failing output to locate from", "BELOW_BAR": "no suspect cleared its confidence bar",
                   "LISTING_INCOMPLETE": "no file list", "MISSING_FIELD": "the description is missing",
                   "CUE_OUTSIDE_LISTING": "the output points outside the file list"}.get(mr.get("code"), mr.get("code"))
            lines.append(f"Not located: {why}.")
    else:
        lines.append(f"Verdict: {state or 'none'}.")
    if result.get("judgment_id"):
        lines.append(f"Reference {result['judgment_id']}.")
    out = Answer(" ".join(lines)[:4000])
    out.raw = result if len(json.dumps(result, default=str)) <= RAW_MAX else {"note": "answer too long to keep",
                                                                                 "verdict": result.get("verdict")}
    return out


def receipt(seat: str, question: str, answer: str) -> None:
    """One 5-field line appended to the bundle's usage_log.jsonl: the bundle's rule for every seat use."""
    line = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "seat": seat,
            "question": question[:300], "output_summary": " ".join(answer.split())[:300],
            "decision_changed": "none: advice only, recorded with the run"}
    with open(CFG.bundle_dir / "usage_log.jsonl", "a") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
