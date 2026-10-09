"""The rung test writer: drafts ONE failing test for a ladder rung, places it as a NEW file beside the code it tests,
runs it in the sandbox with the network off, and classifies the result. What differs by language (where code and
tests live, the framework, the checks on a draft) comes from lang.py; this file is the same for every repo.

Built from the by-hand run of vercel/ai #21439 (2026-10-07), which reproduced the bug in exactly this shape: a new
test beside the provider's own streaming tests, using that file's mock server and helpers; rung 1 fed made-up
chunks, rung 2 a recorded fixture from `__fixtures__/` cut mid tool call.

Decided in code, not left to the model ("prose is not a control"):
  WHERE   the test goes: beside the example test, as da-repro-<issue>-<rung>-<n>.test.ts (Python:
          test_da_repro_<issue>_<rung>_<n>.py); no existing file is edited
  WHAT    a rung means: the integration rung must read a recorded fixture from __fixtures__ (a real stream's format)
  HOW     it is judged: the sandbox's own exit code and output, through ladder.classify()
The model writes only the body of the test.
"""
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import ladder, lang as langs
from .models import write

MAX_SNIPPET_LINES = 220
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class WriterRefused(ValueError):
    """The draft broke a rule the code enforces; recorded as an ERROR attempt (it counts toward the cap)."""


@dataclass
class Context:
    package: str                      # e.g. "openai-compatible" (the package folder's name)
    source: str                       # repo-relative path of the most relevant source file
    snippets: str                     # numbered source lines around every anchor hit
    example_test: str                 # repo-relative path of the test file beside it
    example_header: str               # its imports and setup (everything before the first describe)
    example_case: str                 # one existing streaming test from it, as a pattern
    fixtures: list = field(default_factory=list)  # repo-relative recorded fixtures beside it
    fixture_best: str = ""            # the one or two most relevant (by the focus's words and the helpers' formats)
    fixture_sample: str = ""          # their first lines
    anchors: list = field(default_factory=list)
    ranking: list = field(default_factory=list)   # (file, score) for the record
    extra: str = ""                   # the brief from Gather context: discussion, linked items, shared code, history
    package_dir: str = ""             # e.g. "packages/openai-compatible", "libs/partners/openai", "." (one package)
    # the person's optional pointers (Isha 2026-10-08): files to look in for the cause, a test file to write into
    look_in: list = field(default_factory=list)          # pointed-to source files found in the code (searched first)
    look_in_missing: list = field(default_factory=list)  # pointed-to files that are not source files in the code
    test_into: str = ""               # the unit test is added to this existing test file, as new cases at its end
    test_into_note: str = ""          # why a pointed-to test file was not used (not found, not a test file)


# ── locate: deterministic, $0 ────────────────────────────────────────────────────────────────────
_ANCHOR = re.compile(r"`([^`\n]{4,80})`|\"([^\"\n]{8,80})\"")


def anchors(text: str) -> list[str]:
    """Exact strings worth searching for: code spans and quoted strings (error messages, names)."""
    out = []
    for m in _ANCHOR.finditer(text):
        s = next(g for g in m.groups() if g).strip()
        if re.fullmatch(r"[\d.\s]+", s) or s.startswith(("gen_", "http")) or re.fullmatch(r"\d+\.\d+\.\d+", s):
            continue
        if s not in out:
            out.append(s)
    return out


def _git_grep(checkout: Path, needle: str, lang: langs.Lang) -> list[tuple[str, int]]:
    r = subprocess.run(["git", "-C", str(checkout), "grep", "-n", "-F", "-e", needle, "--", *lang.pathspec()],
                       capture_output=True, text=True, timeout=60)
    hits = []
    for line in r.stdout.splitlines():
        path, num, _ = line.split(":", 2)
        hits.append((path, int(num)))
    return hits


def _specificity(n_files: int) -> int:
    """A string found in one file points somewhere; one found in 40 points nowhere."""
    return 4 if n_files == 1 else 3 if n_files <= 3 else 2 if n_files <= 10 else 1 if n_files <= 40 else 0


POINTED = 10  # a file the person points to outranks any match by the issue's strings (a rare string scores 4, x2 in focus)


def locate(checkout: Path, issue_text: str, focus: str, profile=None, hints: dict | None = None) -> Context:
    """Rank source files by the issue's exact strings, rare strings counting most and the focus's counting double.
    When the focus names a package, only that package's files compete (the first try, on #21439, let the issue's
    OTHER problem outvote the focus). Files the person points to count most. Then gather the test beside the winner
    (or the person's own test file), its setup, one pattern case, fixtures."""
    checkout, lang, hints = Path(checkout), langs.of(profile), hints or {}
    focus_anchors = anchors(focus)
    found = focus_anchors + [a for a in anchors(issue_text) if a not in focus_anchors]
    score, lines = {}, {}
    for a in found:
        hits = _git_grep(checkout, a, lang)
        weight = _specificity(len({p for p, _ in hits})) * (2 if a in focus_anchors else 1)
        if not weight:
            continue
        for path, num in hits:
            pr = 1 if weight >= 3 else 2
            lines.setdefault(path, {})[num] = min(pr, lines.get(path, {}).get(num, pr))
        for path in {p for p, _ in hits}:
            score[path] = score.get(path, 0) + weight
    pkgs = {Path(d).name: d for d in lang.package_dirs(checkout, need_marker=False) if d != "."}
    named = [pkgs[k] for k in pkgs if re.search(rf"\b{re.escape(k)}\b|\b{re.escape(k.replace('-', ' '))}\b", focus, re.I)]
    words = {w for w in re.findall(r"\b[a-z][a-zA-Z]{4,}\b", focus)}
    if named:  # the focus names where to look: search there, by its exact strings and the words it uses as code
        listed = subprocess.run(["git", "-C", str(checkout), "ls-files", "--", *lang.pathspec()],
                                capture_output=True, text=True, timeout=60).stdout.split()
        for path in (p for p in listed if lang.package_of(p) in named):
            text = (checkout / path).read_text(errors="ignore")
            code = sum(1 for w in words if re.search(rf"\.{w}\b|\b{w}\(", text))
            if code or path in score:
                score[path] = score.get(path, 0) + code
        score = {p: v for p, v in score.items() if lang.package_of(p) in named} or score
    # a function or class the focus names counts most where it is DEFINED, not where it is called (dry run on
    # langchain 2026-10-08: `merge_lists` tied across its four callers and lost to them on name order)
    for a in focus_anchors:
        if re.fullmatch(r"[A-Za-z_$][\w$]{3,80}", a):
            for pattern in lang.definition_patterns(a):
                got = subprocess.run(["git", "-C", str(checkout), "grep", "-n", "-E", pattern, "--", *lang.pathspec()],
                                     capture_output=True, text=True, timeout=60).stdout
                for hit in got.splitlines():
                    path, num, _ = hit.split(":", 2)
                    if path in score:
                        score[path] += 3
                        lines.setdefault(path, {})[int(num)] = 0
    look_in, look_missing = [], []
    for p in hints.get("look_in") or []:
        if (checkout / p).is_file() and lang.is_source(p):
            score[p] = score.get(p, 0) + POINTED
            look_in.append(p)
        else:
            look_missing.append(p)
    if not score:
        raise WriterRefused("no source file contains any exact string from the issue")
    ranking = sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))
    source = ranking[0][0]
    src_text = (checkout / source).read_text()
    # focus words the source uses as code (flush(, .flush, flush:) are anchors too
    for word in words:
        for m in re.finditer(rf"\.{word}\b|\b{word}\(", src_text):
            lines.setdefault(source, {})[src_text.count("\n", 0, m.start()) + 1] = 0
    snippets = _snippets(src_text, lines.get(source, {}))

    test_into, into_note, want = "", "", (hints.get("test_in") or "").strip()
    if want:
        if not (checkout / want).is_file():
            into_note = "not found in the code"
        elif not langs.is_test_path(want):
            into_note = "not a test file by its name"
        else:
            test_into = want
    example = test_into or lang.example_test(checkout, source)
    test_text = (checkout / example).read_text()
    header = lang.header(test_text)

    fixtures_dir = (checkout / source).parent / "__fixtures__"
    fixtures = sorted(str(p.relative_to(checkout)) for p in fixtures_dir.glob("*")) if fixtures_dir.exists() else []
    fwords = set(w.lower() for w in re.findall(r"[a-zA-Z]{4,}", focus))
    def fit(f):  # the focus's words in its name, and a format the test file's own helpers read (e.g. .chunks.txt)
        suffix = "".join(Path(f).suffixes)
        return (sum(w in f.lower().replace("-", " ") for w in fwords), suffix in header)
    top = sorted(fixtures, key=fit, reverse=True)[:2]  # a tie is real (two recorded formats): show both, writer picks
    best = ", ".join(top)
    sample = "\n\n".join(f"--- {f}\n" + "\n".join(l[:400] for l in (checkout / f).read_text().splitlines()[:10])
                         for f in top)
    pkg_dir = lang.package_of(source)
    return Context(package=Path(pkg_dir).name if pkg_dir != "." else "", source=source, snippets=snippets,
                   example_test=str(example), example_header=header.strip(),
                   example_case=lang.pattern_case(test_text, focus), fixtures=fixtures, fixture_best=best,
                   fixture_sample=sample, anchors=found, ranking=ranking[:5], package_dir=pkg_dir,
                   look_in=look_in, look_in_missing=look_missing, test_into=test_into, test_into_note=into_note)


def _snippets(text: str, hit_lines: dict, pad: int = 14) -> str:
    """Windows around hit lines, most important first (focus code words, then rare strings), within the budget."""
    src = text.splitlines()
    keep = set()
    for n in sorted(hit_lines, key=lambda k: (hit_lines[k], k)):
        window = set(range(max(1, n - pad), min(len(src), n + pad) + 1))
        if len(keep | window) > MAX_SNIPPET_LINES:
            continue
        keep |= window
    out, prev = [], None
    for n in sorted(keep):
        if prev is not None and n != prev + 1:
            out.append("   …")
        out.append(f"{n:5d}  {src[n - 1]}")
        prev = n
    return "\n".join(out)


# ── write: the only model call ───────────────────────────────────────────────────────────────────
def system(lang: langs.Lang, into: str = "") -> str:
    if into:  # the person named the test file: new cases at its end, nothing else in it changes
        return f"""You add NEW {lang.framework} test cases to the END of an existing test file ({into}). They reproduce ONE reported
problem (the FOCUS) on the CURRENT, unfixed code.
Rules:
- Reproduce ONLY the FOCUS. The issue may describe other problems: ignore them completely.
- The new cases must FAIL on the current code, and fail with the FOCUS symptom (assert the correct behaviour).
- Write ONLY what is added: the file's imports, mocks and helpers (its setup, shown to you) are already there. Never
  repeat or re-declare anything it has. If you need one more import, put it first in your block.
{lang.rules()}
Reply in exactly this shape:
SYMPTOM: <one line: what the failing assertion will show on the current code>
```{lang.fence}
<only the new cases (and any one extra import), to be appended to the file>
```"""
    return f"""You write ONE {lang.framework} test file that reproduces ONE reported problem (the FOCUS) on the CURRENT, unfixed code.
Rules:
- Reproduce ONLY the FOCUS. The issue may describe other problems: ignore them completely.
- The test must FAIL on the current code, and fail with the FOCUS symptom (assert the correct behaviour).
{lang.rules()}
Reply in exactly this shape:
SYMPTOM: <one line: what the failing assertion will show on the current code>
```{lang.fence}
<the whole test file>
```"""


SYSTEM = system(langs.JS())  # vercel/ai's (kept for the record of what its runs were told)

RUNG_RULES = {
    "unit": "RUNG 1 (unit): feed made-up input to the smallest code path that shows the bug. No fixture files.",
    "integration": ("RUNG 2 (integration): build the input from a RECORDED fixture in __fixtures__ (read it with "
                    "fs.readFileSync), changed only as the issue describes (e.g. cut the stream mid tool call). "
                    "Do not invent the stream's format: take it from the fixture."),
}


ISSUE_BUDGET = 12_000


def issue_for_writer(text: str) -> str:
    """The whole issue when it fits; else the start of its prose and its code blocks (the reproduction) whole.
    #22085 run 2026-10-09: the issue was 4,861 characters and the writer saw 3,000; the repro was cut just before the
    lines that show the bug, so its test missed it."""
    from .issue_text import SECRET_VALUE
    text = SECRET_VALUE.sub("[REDACTED-KEY]", text or "")
    if len(text) <= ISSUE_BUDGET:
        return text
    code = "\n\n".join(re.findall(r"```.*?```", text, re.S))[: ISSUE_BUDGET - 3000]
    return text[: ISSUE_BUDGET - len(code) - 60] + "\n…(cut)\n\nCODE FROM THE ISSUE, WHOLE:\n" + code


def messages(issue_title: str, issue_text: str, focus: str, rung: ladder.Rung, history: list, ctx: Context,
             lang: langs.Lang | None = None) -> list:
    past = ""
    for a in history:
        past += f"\n- attempt {a.n} ({a.rung}): {a.outcome}. {a.evidence[:600]}"
        if a.outcome == ladder.GREEN:
            past += ("\n  That test PASSED on the current code, so it did not trigger the bug. Follow the issue's own "
                     "reproduction step by step (the same inputs, the same calls, the same check), then assert the "
                     "correct behaviour.")
    fixture = (f"\n\nRecorded fixtures beside it: {', '.join(Path(f).name for f in ctx.fixtures)}\n"
               f"The most relevant, first lines (pick one; the setup above has a helper for each format):\n"
               f"{ctx.fixture_sample}"
               if ctx.fixtures and rung.name == "integration" else "")
    rule = RUNG_RULES[rung.name]
    if lang is not None and lang.key == "python":  # "fixture" means something else to pytest
        rule = rule.replace("No fixture files.", "No recorded data files (fixtures from the setup are fine).")
    user = f"""FOCUS (the ONE problem to reproduce): {focus}

{rule}

BACKGROUND, the whole issue "{issue_title}" (any other problem in it is OUT OF SCOPE):
{issue_for_writer(issue_text)}

{('CONTEXT GATHERED BEFORE THIS STEP:' + chr(10) + ctx.extra[:12000] + chr(10) + chr(10)) if ctx.extra else ''}MOST RELEVANT SOURCE: {ctx.source} (lines around the issue's exact strings)
{ctx.snippets}

SETUP OF THE TEST FILE BESIDE IT ({ctx.example_test}):
{ctx.example_header[:6000]}

A PATTERN TEST FROM THAT FILE:
{ctx.example_case[:3500]}{fixture}

EARLIER ATTEMPTS:{past or " none"}"""
    into = getattr(ctx, "test_into", "") if rung.name == "unit" else ""
    if into:
        user = user.replace("SETUP OF THE TEST FILE BESIDE IT", "SETUP OF THE FILE YOU ADD TO", 1)
    return [("system", system(lang or langs.JS(), into)), ("user", user)]


# an unclosed block (cut off) still parses
_TS_BLOCK = re.compile(r"```(?:ts|typescript|tsx|js|javascript|jsx|python|py)?[ \t]*\n(.*?)(?:```|\Z)", re.S)


def parse(reply: str) -> tuple[str, str]:
    m = _TS_BLOCK.search(reply)
    if not m or not m.group(1).strip():
        raise WriterRefused("the reply has no code block")
    symptom = re.search(r"SYMPTOM:\s*(.+)", reply)
    # trial 2026-10-07: the model put its SYMPTOM line inside the code twice, which broke compilation
    code = re.sub(r"^\s*SYMPTOM:.*$", "", m.group(1), flags=re.M).strip()
    return code + "\n", (symptom.group(1).strip() if symptom else "")


# Words every failure prints, which prove nothing. Trial 2026-10-07: "error" matched the label AssertionError itself.
GENERIC = {"error", "errors", "expected", "received", "undefined", "null", "true", "false", "object", "string",
           "number", "message", "value", "values", "result", "type", "data", "test", "assert", "equal"}


def symptom_terms(focus: str) -> list[str]:
    """The focus's own code strings (e.g. `tool-call`, `input: ""` → tool-call, input): a reproduction's failure must
    show at least one. Empty when the focus has none (then the check can't run and says so)."""
    terms = []
    for a in anchors(focus):
        for t in re.findall(r"[A-Za-z][\w-]{3,}", a):
            if t.lower() not in GENERIC and t not in terms:
                terms.append(t)
    return terms


ASSERT_WORDS = {"deeply", "strictly", "equal", "equals", "actual", "toequal", "tostrictequal", "tobe", "deepequal",
                "strictequal", "assertionerror", "err_assertion", "length", "message", "name", "type", "value", "values"}
_QUOTED = re.compile(r"""['"`]([^'"`\n]{3,60})['"`]""")
_KEY = re.compile(r"(?<![\w.])([A-Za-z_][\w]{2,40})\s*:(?!:)")


def evidence_terms(evidence: str) -> list[str]:
    """What the reproducing test's own failure showed: the quoted values and object keys of its failed assertion,
    assertion boilerplate left out. Review of run #22085 (2026-10-09): the issue quoted no code, so its words gave
    nothing to check, while the test's failure said `unpaired: ['ws_1']`."""
    terms = []
    for a in assertion_text(evidence or ""):
        for t in _QUOTED.findall(a) + _KEY.findall(a):
            t = t.strip()
            if len(t) >= 3 and t.lower() not in GENERIC | ASSERT_WORDS and not t.isdigit() and t not in terms:
                terms.append(t)
    return terms[:12]


def check_terms(focus: str, evidence: str = "") -> list[str]:
    """Everything a failure is checked against: the focus's own code strings, then what the reproducing test showed."""
    terms = symptom_terms(focus)
    return terms + [t for t in evidence_terms(evidence) if t not in terms]


def right_reason(focus: str, output: str, evidence: str = "") -> bool | None:
    """Does the failing assertion show the bug? Checked against the focus's code strings and, once the bug has been
    shown, against what the reproducing test's failure showed (`evidence`). Trial 2026-10-07: a test of the issue's
    OTHER problem failed on a wrong expectation and read as RED. None = nothing to check against (can't tell)."""
    terms = check_terms(focus, evidence)
    if not terms:
        return None
    said = [re.sub(r"AssertionError", "", a).lower() for a in assertion_text(output)]
    return any(t.lower() in a for a in said for t in terms)


def shows_bug(focus: str, output: str, evidence: str = "") -> tuple[bool, bool]:
    """One rule for every step (review of run #22085, 2026-10-09: "can't tell" counted as yes in Show the bug and as
    no in the guard, which threw away a correct guard). → (shows the bug, whether that could be checked). When it
    can't be checked, a failure on an assertion counts and a crash does not; the page says the check wasn't possible."""
    rr = right_reason(focus, output, evidence)
    if rr is None:
        return bool(assertion_text(output)), False
    return rr, True


NOT_CHECKED = "symptom check: not possible (the issue quotes no code); counted because it failed on an assertion"


def assertion_text(output: str) -> list[str]:
    """Only what the failed assertions SAY about what was RECEIVED: the message line plus the "+" (received) side of
    its diff, never the expected side and never the source lines the runner prints around them.
    Trial 2026-10-07 (1): "tool-call" in a code comment beside the assertion made a wrong-reason failure look right.
    Trial 2026-10-07 (2): vitest abbreviates the message (`{ toolCallParts: [ { …(4) } ] }`) and the evidence sits in
    the diff after a blank line; stopping at the blank line called a correct guard broken."""
    clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
    found = []
    for m in re.finditer(r"^.*AssertionError.*$", clean, re.M):
        block = [m.group(0)]
        for line in clean[m.end() + 1:].splitlines()[:60]:
            if re.match(r"\s*(❯|\d+\||at |>\s|\S+\.(ts|js|py):\d+|⎯|FAIL\b)", line):
                break  # vitest's source pointer / numbered source / stack frame / separator / next failure
            if line.startswith("+") and not line.startswith("+ Received"):
                block.append(line)
        found.append("\n".join(block))
    found += [l for l in clean.splitlines() if l.startswith("E   ")]  # pytest's assertion explanation lines
    found += _tap_errors(clean)
    return found


def _tap_errors(clean: str) -> list[str]:
    """node:test's TAP: the message and the "+" (actual) lines sit indented under `error: |-` (blank lines inside), the
    value under `actual:`."""
    lines, out = clean.splitlines(), []
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)(error|actual):(.*)$", line)
        if not m or not re.match(r"^\s+\w", line):
            continue
        indent, body = len(m.group(1)), []
        if m.group(3).strip() and m.group(3).strip() not in ("|-", "|", ">-", ">"):
            body.append(m.group(3).strip())
        for nxt in lines[i + 1:]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                break
            body.append(nxt.strip())
        body = [b for b in body if b]
        if m.group(2) == "error":
            out.append("\n".join(body[:1] + [b for b in body[1:] if b.startswith("+") and not b.startswith("+ actual")]))
        else:
            out.append("actual: " + " ".join(body))
    return out


def validate(content: str, rung: ladder.Rung, lang: langs.Lang | None = None) -> None:
    (lang or langs.JS()).validate(content, rung.name)
    if len(content) > 20_000:
        raise WriterRefused("the test file is too long to be one focused reproduction")


# ── one attempt ──────────────────────────────────────────────────────────────────────────────────
def attempt(state: dict, rung: ladder.Rung, n: int, history: list, ctx: Context, checkout: Path, profile,
            run_cmd=None, drafts: Path | None = None, step: str = "reproduce", label: str = "",
            proof_dir: Path | None = None) -> ladder.Attempt:
    """Draft (one model call, metered), validate, write beside the example test, run network-off, classify."""
    from .sandbox import run_in_sandbox
    issue, lang = state["issue"], langs.of(profile)
    into = getattr(ctx, "test_into", "") if rung.name == "unit" else ""  # the person's own test file, when named
    rel = into or lang.new_test(ctx.example_test, "repro", issue["number"], label or rung.name, n)
    try:
        focus = state.get("focus") or issue["title"]
        msg, _ = write(state, step, messages(issue["title"], state.get("issue_text") or issue.get("body", ""),
                                                    focus, rung, history, ctx, lang), max_tokens=4000)
        if drafts:
            Path(drafts).mkdir(parents=True, exist_ok=True)
            (Path(drafts) / f"{n}-{label or rung.name}.md").write_text(str(msg.content))  # every raw reply, for the record
        content, symptom = parse(str(msg.content))
        validate(content, rung, lang)
    except WriterRefused as e:
        return ladder.Attempt(rung=rung.name, n=n, outcome=ladder.ERROR, evidence=f"writer refused: {e}", test_path="")
    base = original(Path(checkout), into) if into else ""
    written = (base.rstrip("\n") + "\n\n" + content) if into else content
    (Path(checkout) / rel).write_text(written)
    cmd = lang.test_command((lang.package_of(rel) if into else ctx.package_dir) or lang.package_of(rel), rel)
    r = (run_cmd or run_in_sandbox)(cmd, Path(checkout), network=False, timeout=300, image=profile.image)
    out = (r.stdout or "") + (r.stderr or "")
    outcome, line = ladder.classify(profile.language, r.returncode, out)
    if proof_dir:
        write_proof(Path(proof_dir), rel, written, cmd, r.returncode, out, outcome, line, profile, Path(checkout),
                    name=f"try-{n}-{Path(rel).name}" if into else None)
    detail = _detail(out)
    r0 = state.get("repro") or {}   # once the bug has been shown, a later test is checked against that failure too
    known = "" if step == "reproduce" else (r0.get("oracle_evidence") or r0.get("evidence") or "")
    focus_now = state.get("focus") or issue["title"]
    checked = None
    if outcome == ladder.RED:
        shown, checked = shows_bug(focus_now, out, known)
        if not shown:
            terms = ", ".join(check_terms(focus_now, known))
            outcome, line = ladder.ERROR, (f"RED for another reason: the failure shows none of these: {terms}. A "
                                           f"reproduction must fail with the FOCUS symptom. Got: {line}")
    if into and outcome != ladder.RED:  # only cases that show the bug stay in the person's file
        (Path(checkout) / rel).write_text(base)
    return ladder.Attempt(rung=rung.name, n=n, outcome=outcome,
                          evidence=(f"{line}" + (f"\n{detail}" if detail else "") +
                                    (f"\n{NOT_CHECKED}" if outcome == ladder.RED and checked is False else "") +
                                    (f"\nwriter's symptom: {symptom}" if symptom else ""))[:1500],
                          test_path=rel)


def original(checkout: Path, rel: str) -> str:
    """A tracked file as the commit has it (a crashed try may have left cases in the working copy)."""
    r = subprocess.run(["git", "-C", str(checkout), "show", f"HEAD:{rel}"], capture_output=True, text=True, timeout=60)
    return r.stdout if r.returncode == 0 else (Path(checkout) / rel).read_text()


def tracked(checkout: Path, rel: str) -> bool:
    return subprocess.run(["git", "-C", str(checkout), "ls-files", "--error-unmatch", "--", rel],
                          capture_output=True, timeout=60).returncode == 0


def write_proof(proof_dir: Path, rel: str, content: str | None, cmd: str, code: int, out: str, outcome: str, line: str,
                profile, checkout: Path, name: str | None = None) -> Path:
    """The proof that a test fails (or passes) on the unfixed code, as plain text anyone can check: the test's
    fingerprint, the code it ran against, the exact command, where and when it ran, the exit code and the whole
    output. runs/<id>/proof/<test file name>.txt (Isha 2026-10-08: "attach proof that the bug reproduces")."""
    import hashlib
    from datetime import datetime, timezone
    from .config import CFG
    if CFG.sandbox_backend == "e2b":
        from .sandbox_e2b import template_for
        where = f"E2B sandbox from template {template_for(checkout)}"
    else:
        where = f"Docker container from {profile.image}"
    proof_dir.mkdir(parents=True, exist_ok=True)
    f = proof_dir / f"{name or Path(rel).name}.txt"
    f.write_text(f"test file   {rel}\n"
                 f"sha256      {hashlib.sha256(content.encode()).hexdigest() if content is not None else '-'}\n"
                 f"code        {profile.repo} at {(profile.base_commit or '')[:12]}: source unchanged, only new test files\n"
                 f"ran         {cmd}\n"
                 f"where       {where}, internet off\n"
                 f"when        {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
                 f"exit code   {code}\n"
                 f"verdict     {outcome}: {line}\n"
                 f"--- output ---\n{ANSI.sub('', out)[-40000:]}")
    if content is not None:  # the test as a change to the code, git style: a new file, or the cases added to a file
        from . import diffview
        try:
            diff = (diffview._git(checkout, "diff", "--no-color", "--", rel) if tracked(checkout, rel) else "") or \
                diffview.new_file_diff(rel, content)
        except (OSError, subprocess.SubprocessError):
            diff = diffview.new_file_diff(rel, content)
        f.with_suffix(".diff").write_text(diff)
    return f


def counts(output: str) -> dict:
    """{"passed": n, "failed": n} from the runner's own summary (vitest, jest, pytest, node:test, plain scripts); {} when
    it printed none."""
    t = ANSI.sub("", output)
    for rx in (r"^\s*Tests:?\s+(?:(?P<f>\d+) failed\s*[|,]\s*)?(?P<p>\d+) passed",       # vitest, jest
               r"=+ (?:(?P<f>\d+) failed, )?(?P<p>\d+) passed",                         # pytest
               r"(?P<p>\d+) passed, (?P<f>\d+) failed"):                                # plain scripts
        found = list(re.finditer(rx, t, re.M))
        if found:
            return {"passed": sum(int(m.group("p")) for m in found), "failed": sum(int(m.group("f") or 0) for m in found)}
    tap_p, tap_f = re.findall(r"^# pass (\d+)", t, re.M), re.findall(r"^# fail (\d+)", t, re.M)  # node:test
    if tap_p:
        return {"passed": sum(map(int, tap_p)), "failed": sum(map(int, tap_f))}
    return {}


def read_proof(f: Path) -> dict:
    """The proof file back as {field: value, "output": ...}; {} when there is none (runs before 2026-10-08)."""
    if not Path(f).exists():
        return {}
    head, _, output = Path(f).read_text(errors="replace").partition("--- output ---\n")
    got = {m.group(1).replace(" ", "_"): m.group(2).strip() for m in re.finditer(r"^(\w[\w ]*?)[ \t]{2,}(.*)$", head, re.M)}
    d = Path(f).with_suffix(".diff")
    return {**got, "output": output, **({"diff": d.read_text(errors="replace")} if d.exists() else {})}


def _detail(out: str) -> str:
    """The lines after the first assertion or error: what the next attempt (and a person) needs to see."""
    m = re.search(r"(AssertionError|Error:|SyntaxError|TypeError)[^\n]*\n(?:.*\n){0,12}", out)
    return re.sub(r"\x1b\[[0-9;]*m", "", m.group(0)).strip() if m else ""
