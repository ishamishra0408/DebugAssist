"""The rung test writer: drafts ONE failing test for a ladder rung, places it as a NEW file beside the code it tests,
runs it in the sandbox with the network off, and classifies the result.

Built from the by-hand run of vercel/ai #21439 (2026-10-07), which reproduced the bug in exactly this shape: a new
test beside the provider's own streaming tests, using that file's mock server and helpers; rung 1 fed made-up
chunks, rung 2 a recorded fixture from `__fixtures__/` cut mid tool call.

Decided in code, not left to the model ("prose is not a control"):
  WHERE   the test goes: beside the example test, as da-repro-<issue>-<rung>-<n>.test.ts; no existing file is edited
  WHAT    a rung means: the integration rung must read a recorded fixture from __fixtures__ (a real stream's format)
  HOW     it is judged: the sandbox's own exit code and output, through ladder.classify()
The model writes only the body of the test.
"""
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import ladder
from .models import write

MAX_SNIPPET_LINES = 220
EXCLUDE = [":!*.test.ts", ":!*.test.tsx", ":!*__fixtures__*", ":!*__snapshots__*", ":!*.md"]


class WriterRefused(ValueError):
    """The draft broke a rule the code enforces; recorded as an ERROR attempt (it counts toward the cap)."""


@dataclass
class Context:
    package: str                      # e.g. "openai-compatible"
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


def _git_grep(checkout: Path, needle: str) -> list[tuple[str, int]]:
    r = subprocess.run(["git", "-C", str(checkout), "grep", "-n", "-F", "-e", needle, "--", "packages/*/src/**",
                        *EXCLUDE], capture_output=True, text=True, timeout=60)
    hits = []
    for line in r.stdout.splitlines():
        path, num, _ = line.split(":", 2)
        hits.append((path, int(num)))
    return hits


def _specificity(n_files: int) -> int:
    """A string found in one file points somewhere; one found in 40 points nowhere."""
    return 4 if n_files == 1 else 3 if n_files <= 3 else 2 if n_files <= 10 else 1 if n_files <= 40 else 0


def locate(checkout: Path, issue_text: str, focus: str) -> Context:
    """Rank source files by the issue's exact strings, rare strings counting most and the focus's counting double.
    When the focus names a package, only that package's files compete (the first try, on #21439, let the issue's
    OTHER problem outvote the focus). Then gather the test beside the winner, its setup, one pattern case, fixtures."""
    checkout = Path(checkout)
    focus_anchors = anchors(focus)
    found = focus_anchors + [a for a in anchors(issue_text) if a not in focus_anchors]
    score, lines = {}, {}
    for a in found:
        hits = _git_grep(checkout, a)
        weight = _specificity(len({p for p, _ in hits})) * (2 if a in focus_anchors else 1)
        if not weight:
            continue
        for path, num in hits:
            pr = 1 if weight >= 3 else 2
            lines.setdefault(path, {})[num] = min(pr, lines.get(path, {}).get(num, pr))
        for path in {p for p, _ in hits}:
            score[path] = score.get(path, 0) + weight
    pkgs = [p.name for p in (checkout / "packages").iterdir() if p.is_dir()]
    named = [k for k in pkgs if re.search(rf"\b{re.escape(k)}\b|\b{re.escape(k.replace('-', ' '))}\b", focus, re.I)]
    words = {w for w in re.findall(r"\b[a-z][a-zA-Z]{4,}\b", focus)}
    if named:  # the focus names where to look: search there, by its exact strings and the words it uses as code
        for k in named:
            for path in subprocess.run(["git", "-C", str(checkout), "ls-files", f"packages/{k}/src/**", *EXCLUDE],
                                       capture_output=True, text=True, timeout=60).stdout.split():
                text = (checkout / path).read_text(errors="ignore")
                code = sum(1 for w in words if re.search(rf"\.{w}\b|\b{w}\(", text))
                if code or path in score:
                    score[path] = score.get(path, 0) + code
        score = {p: v for p, v in score.items() if p.split("/")[1] in named} or score
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

    example = Path(source).with_name(Path(source).stem + ".test.ts")
    if not (checkout / example).exists():
        tests = sorted((checkout / source).parent.glob("*.test.ts"), key=lambda p: -p.stat().st_size)
        if not tests:
            raise WriterRefused(f"no test file beside {source} to follow")
        example = tests[0].relative_to(checkout)
    test_text = (checkout / example).read_text()
    first = re.search(r"^describe\(", test_text, re.M)
    header = test_text[:first.start()] if first else test_text[:4000]

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
    return Context(package=source.split("/")[1], source=source, snippets=snippets, example_test=str(example),
                   example_header=header.strip(), example_case=_pattern_case(test_text), fixtures=fixtures,
                   fixture_best=best, fixture_sample=sample, anchors=found, ranking=ranking[:5])


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


def _pattern_case(test_text: str) -> str:
    """One existing streaming test, whole, as the pattern to follow (the by-hand run copied exactly this shape)."""
    lines = test_text.splitlines()
    for i, l in enumerate(lines):
        if re.match(r"\s+it\(", l) and "stream-chunks" in "\n".join(lines[i:i + 30]):
            indent = len(l) - len(l.lstrip())
            for j in range(i + 1, min(len(lines), i + 120)):
                if lines[j].startswith(" " * indent + "});"):
                    return "\n".join(lines[i:j + 1])
    return ""


# ── write: the only model call ───────────────────────────────────────────────────────────────────
SYSTEM = """You write ONE vitest test file that reproduces ONE reported problem (the FOCUS) on the CURRENT, unfixed code.
Rules:
- Reproduce ONLY the FOCUS. The issue may describe other problems: ignore them completely.
- The test must FAIL on the current code, and fail with the FOCUS symptom (assert the correct behaviour).
- Start with the setup you are given (imports, mock server, model, helpers), adjusted only as needed. The file is
  saved beside the example test, so its relative imports work unchanged.
- No network, no API keys, no environment variables: use the mock server exactly as the pattern test does.
- Fixture paths are relative to the package directory (vitest runs there), e.g. 'src/chat/__fixtures__/x.chunks.txt'.
- Make the failing assertion SHOW the evidence: compare the actual parts or values (toStrictEqual / toEqual on the
  list of stream parts), never a bare count or boolean, so the failure message prints what went wrong.
- Do not test anything else. One describe block, one to three it() cases.
Reply in exactly this shape:
SYMPTOM: <one line: what the failing assertion will show on the current code>
```ts
<the whole test file>
```"""

RUNG_RULES = {
    "unit": "RUNG 1 (unit): feed made-up input to the smallest code path that shows the bug. No fixture files.",
    "integration": ("RUNG 2 (integration): build the input from a RECORDED fixture in __fixtures__ (read it with "
                    "fs.readFileSync), changed only as the issue describes (e.g. cut the stream mid tool call). "
                    "Do not invent the stream's format: take it from the fixture."),
}


def messages(issue_title: str, issue_text: str, focus: str, rung: ladder.Rung, history: list, ctx: Context) -> list:
    past = ""
    for a in history:
        past += f"\n- attempt {a.n} ({a.rung}): {a.outcome}. {a.evidence[:600]}"
    fixture = (f"\n\nRecorded fixtures beside it: {', '.join(Path(f).name for f in ctx.fixtures)}\n"
               f"The most relevant, first lines (pick one; the setup above has a helper for each format):\n"
               f"{ctx.fixture_sample}"
               if ctx.fixtures and rung.name == "integration" else "")
    user = f"""FOCUS (the ONE problem to reproduce): {focus}

{RUNG_RULES[rung.name]}

BACKGROUND, the whole issue "{issue_title}" (any other problem in it is OUT OF SCOPE):
{issue_text[:3000]}

MOST RELEVANT SOURCE: {ctx.source} (lines around the issue's exact strings)
{ctx.snippets}

SETUP OF THE TEST FILE BESIDE IT ({ctx.example_test}):
{ctx.example_header[:6000]}

A PATTERN TEST FROM THAT FILE:
{ctx.example_case[:3500]}{fixture}

EARLIER ATTEMPTS:{past or " none"}"""
    return [("system", SYSTEM), ("user", user)]


_TS_BLOCK = re.compile(r"```(?:ts|typescript)?[ \t]*\n(.*?)(?:```|\Z)", re.S)  # an unclosed block (cut off) still parses


def parse(reply: str) -> tuple[str, str]:
    m = _TS_BLOCK.search(reply)
    if not m or not m.group(1).strip():
        raise WriterRefused("the reply has no ```ts block")
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


def right_reason(focus: str, output: str) -> bool | None:
    """Does the failing assertion show the focus's symptom? Trial 2026-10-07: a test of the issue's OTHER problem
    failed on a wrong expectation and read as RED. None = the focus gives nothing to check against."""
    terms = symptom_terms(focus)
    if not terms:
        return None
    said = [re.sub(r"AssertionError", "", a).lower() for a in assertion_text(output)]
    return any(t.lower() in a for a in said for t in terms)


def assertion_text(output: str) -> list[str]:
    """Only what the failed assertions SAY: the message and its expected/received diff, never the source lines the
    runner prints around them. Trial 2026-10-07: "tool-call" in a code comment beside the assertion made a wrong-reason
    failure look right."""
    clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
    found = []
    for m in re.finditer(r"^.*AssertionError.*$", clean, re.M):
        block = [m.group(0)]
        for line in clean[m.end() + 1:].splitlines()[:30]:
            if re.match(r"\s*(❯|\d+\||at |>\s|\S+\.(ts|js|py):\d+)", line) or not line.strip():
                break  # vitest's source pointer / numbered source lines / a stack frame / pytest's source marker
            block.append(line)
        found.append("\n".join(block))
    found += [l for l in clean.splitlines() if l.startswith("E   ")]  # pytest's assertion explanation lines
    return found


def validate(content: str, rung: ladder.Rung) -> None:
    if "expect(" not in content:
        raise WriterRefused("the test asserts nothing (no expect)")
    if re.search(r"\b(it|describe|test)\.only\(", content):
        raise WriterRefused(".only would hide other cases")
    if re.search(r"process\.env|https?://(?!my\.api\.com|localhost|127\.0\.0\.1)[\w.-]+\.\w", content):
        raise WriterRefused("the test reaches for env vars or a real host; the sandbox has neither")
    if rung.name == "integration" and not ("__fixtures__/" in content and re.search(r"readFile(Sync)?\(", content)):
        raise WriterRefused("the integration rung must read a recorded fixture from __fixtures__")
    if rung.name == "unit" and "__fixtures__/" in content:
        raise WriterRefused("the unit rung uses made-up input, not a recorded fixture")
    if len(content) > 20_000:
        raise WriterRefused("the test file is too long to be one focused reproduction")


# ── one attempt ──────────────────────────────────────────────────────────────────────────────────
def attempt(state: dict, rung: ladder.Rung, n: int, history: list, ctx: Context, checkout: Path, profile,
            run_cmd=None, drafts: Path | None = None, step: str = "reproduce", label: str = "") -> ladder.Attempt:
    """Draft (one model call, metered), validate, write beside the example test, run network-off, classify."""
    from .sandbox import run_in_sandbox
    issue = state["issue"]
    name = f"da-repro-{issue['number']}-{label or rung.name}-{n}.test.ts"
    rel = str(Path(ctx.example_test).with_name(name))
    try:
        focus = state.get("focus") or issue["title"]
        msg, _ = write(state, step, messages(issue["title"], state.get("issue_text") or issue.get("body", ""),
                                                    focus, rung, history, ctx), max_tokens=4000)
        if drafts:
            (Path(drafts) / f"{n}-{label or rung.name}.md").write_text(str(msg.content))  # every raw reply, for the record
        content, symptom = parse(str(msg.content))
        validate(content, rung)
    except WriterRefused as e:
        return ladder.Attempt(rung=rung.name, n=n, outcome=ladder.ERROR, evidence=f"writer refused: {e}", test_path="")
    (Path(checkout) / rel).write_text(content)
    in_pkg = str(Path(rel).relative_to(f"packages/{ctx.package}"))
    cmd = profile.env + profile.test_cmd.format(package=ctx.package, test_path=in_pkg)
    r = (run_cmd or run_in_sandbox)(cmd, Path(checkout), network=False, timeout=300, image=profile.image)
    out = (r.stdout or "") + (r.stderr or "")
    outcome, line = ladder.classify(profile.language, r.returncode, out)
    detail = _detail(out)
    if outcome == ladder.RED and right_reason(state.get("focus") or issue["title"], out) is False:
        terms = ", ".join(symptom_terms(state.get("focus") or ""))
        outcome, line = ladder.ERROR, (f"RED for another reason: the failure shows none of the focus's strings "
                                       f"({terms}). A reproduction must fail with the FOCUS symptom. Got: {line}")
    return ladder.Attempt(rung=rung.name, n=n, outcome=outcome,
                          evidence=(f"{line}" + (f"\n{detail}" if detail else "") +
                                    (f"\nwriter's symptom: {symptom}" if symptom else ""))[:1500],
                          test_path=rel)


def _detail(out: str) -> str:
    """The lines after the first assertion or error: what the next attempt (and a person) needs to see."""
    m = re.search(r"(AssertionError|Error:|SyntaxError|TypeError)[^\n]*\n(?:.*\n){0,12}", out)
    return re.sub(r"\x1b\[[0-9;]*m", "", m.group(0)).strip() if m else ""
