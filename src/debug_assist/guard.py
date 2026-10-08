"""lasting_guard: a check that runs by itself and covers the CLASS of bug, not the one case (allspaw: a condition,
never an instruction).

Built from the by-hand run of #21439, whose guard found the same bug in 6 other providers, and from the full Opus run,
whose correct fix still left two neighbouring triggers (an error mid-stream, the length limit) open.

  siblings   code: every other source line with the same pattern the fix changed (git grep, $0)
  guard      one model call: ONE new test file, parametrised over the class of triggers (it.each), each case asserting
             the correct behaviour
  judged by code, never by the model:
    on the UNFIXED code  it must fail with the focus's symptom: proof it would have caught this bug
    on the FIXED code    each case passing or failing is recorded: a failing case is a part of the class the fix
                         left open, reported, not hidden
The guard file is kept in the run folder, not left in the fix: a test that fails on purpose doesn't belong in a PR's
green suite until the open cases are fixed.
"""
import re
import subprocess
from pathlib import Path

from . import events, ladder, lang as langs
from .budget import BudgetExceeded, TurnCapExceeded
from .fixer import run_one
from .models import write
from .testwriter import WriterRefused, parse, right_reason, validate

GUARD_TRIES = 2
# every case listed, passing ones too: vitest folds an all-green file into one line (Opus run 2026-10-07)
VERBOSE = {"typescript": "--reporter=verbose", "python": langs.Python.VERBOSE}


def _verbose(profile) -> str:
    return langs.of(profile).VERBOSE if profile is not None else VERBOSE["typescript"]


MAX_SIBLING_FILES = 15  # a line in more files than this is ordinary code, not a pattern worth naming


def sibling_sites(checkout: Path, lines, fixed_file: str, profile=None) -> list[str]:
    """Other files containing a distinctive line the fix changed: the same bug, waiting elsewhere. One entry per file,
    "path:line,line". (Opus run 2026-10-07: the first changed line was specific to the fixed file; the second,
    `toolCallTracker.flush();`, is in 6 others.)"""
    per_file: dict[str, set] = {}
    for line in ([lines] if isinstance(lines, str) else list(lines)):
        if not line:
            continue
        got = subprocess.run(["git", "-C", str(checkout), "grep", "-n", "-F", "-e", line, "--",
                              *langs.of(profile).pathspec()], capture_output=True, text=True, timeout=60).stdout
        hits = [l.split(":", 2)[:2] for l in got.splitlines() if not l.startswith(fixed_file + ":")]
        if len({h[0] for h in hits}) > MAX_SIBLING_FILES:
            continue
        for path, num in hits:
            per_file.setdefault(path, set()).add(int(num))
    return [f"{p}:{','.join(map(str, sorted(n)))}" for p, n in sorted(per_file.items())]


def system(lang: langs.Lang) -> str:
    return f"""You write a LASTING GUARD: ONE {lang.framework} test file that fails whenever this CLASS of bug exists, not only the
one case that was reported. {lang.guard_rules()}
(for example every way a stream can end before a value is complete). Each case asserts the CORRECT behaviour and
shows the evidence on failure (compare the actual parts, not counts).
Use only the setup and helpers shown. No network, no env vars. Fixture paths are relative to the package directory.
Reply exactly:
COVERS: <one line: the class of input the cases cover>
```{lang.fence}
<the whole test file>
```"""


SYSTEM = system(langs.JS())  # vercel/ai's


def messages(focus: str, condition: str, cause: dict, fix_patch: str, judge_code: str, header: str,
             conditions_text: str, feedback: str, fixtures: list | None = None, lang: langs.Lang | None = None) -> list:
    user = f"""THE BUG (fixed): {focus}
THE CONDITION THAT LET THIS CLASS SHIP: {condition}
{conditions_text[:2500]}

CAUSE: {cause['file']} lines {cause['lines'][0]}-{cause['lines'][1]}. {cause.get('why', '')}
THE FIX:
{fix_patch[:2500]}

THE TEST THAT REPRODUCED IT (follow its setup):
{judge_code[:6000]}

SETUP OF THE PACKAGE'S OWN TEST FILE:
{header[:4000]}

FIXTURE FILES THAT EXIST (use only these, or made-up inline chunks): {', '.join(Path(f).name for f in (fixtures or [])) or 'none'}
{('YOUR LAST GUARD: ' + feedback) if feedback else ''}"""
    return [("system", system(lang or langs.JS())), ("user", user)]


def failure_blocks(output: str, lang: langs.Lang | None = None) -> dict:
    """Case header → what it printed (vitest's ' FAIL  file > describe > case', pytest's '____ case ____')."""
    return (lang or langs.JS()).failure_blocks(output)


def judged(focus: str, output: str, lang: langs.Lang | None = None) -> dict:
    """Each case, judged on its OWN failure. Dev trial 2026-10-07: one case failed only because it read a fixture
    file that doesn't exist, and was nearly reported as a part of the bug class the fix left open."""
    got, blocks = cases(output, lang), failure_blocks(output, lang)
    out = {"passed": got["passed"], "symptom": [], "broken": []}
    for name in got["failed"]:
        block = next((b for h, b in blocks.items() if name.rstrip("…") in h), "")
        (out["symptom"] if right_reason(focus, block) else out["broken"]).append(
            name if right_reason(focus, block) else f"{name}: {(_first_error(block) or 'no assertion shown')[:200]}")
    return out


def _first_error(block: str) -> str:
    m = re.search(r"^\s*(?:E\s+)?(\w*(?:Error|Exception)\b.*)$", block, re.M)  # pytest prefixes its lines with E
    return m.group(1).strip() if m else ""


def cases(output: str, lang: langs.Lang | None = None) -> dict:
    """Per-case results: {"passed": [...], "failed": [...]} (vitest / jest / pytest, by the repo's language)."""
    return (lang or langs.JS()).cases(output)


def write_guard(state: dict, profile, fixed: Path, unfixed: Path, judge: str, cause: dict, fix_patch: str,
                condition: str, conditions_text: str, header: str, keep_dir: Path, run_cmd=None,
                fixtures: list | None = None) -> dict:
    from .sandbox import run_in_sandbox
    run_cmd = run_cmd or run_in_sandbox
    focus, lang = state.get("focus") or state["issue"]["title"], langs.of(profile)
    rel = lang.new_test(judge, "guard", state["issue"]["number"])
    judge_code = (Path(fixed) / judge).read_text()
    feedback, tries = "", []
    for n in range(1, GUARD_TRIES + 1):
        try:
            msg, _ = write(state, "lasting_guard", messages(focus, condition, cause, fix_patch, judge_code, header,
                                                            conditions_text, feedback, fixtures, lang), max_tokens=4000)
        except (TurnCapExceeded, BudgetExceeded) as e:
            return {"status": "NOT WRITTEN", "why": f"stopped by a cap: {e}", "tries": tries}
        reply = str(msg.content)
        Path(keep_dir).mkdir(parents=True, exist_ok=True)
        (Path(keep_dir) / f"guard-{n}.md").write_text(reply)
        covers = (re.search(r"COVERS:\s*(.+)", reply) or [None, ""])[1].strip()
        try:
            content, _ = parse(reply)
            validate(content, ladder.Rung(0, "guard", "lasting guard"), lang)  # no fixture rule either way
        except WriterRefused as e:
            feedback = f"refused: {e}"
            tries.append({"n": n, "result": feedback})
            events.log("guard_try", key=f"try#{n}", n=n, result=feedback[:300])
            continue
        # 1. on the UNFIXED code: it must catch this bug, for the focus's reason
        (Path(unfixed) / rel).write_text(content)
        r = run_one(Path(unfixed), profile, rel, run_cmd, extra=_verbose(profile))
        out_u = (r.stdout or "") + (r.stderr or "")
        outcome, line = ladder.classify(profile.language, r.returncode, out_u)
        (Path(unfixed) / rel).unlink()
        on_unfixed = judged(focus, out_u, lang)
        last_try = n == GUARD_TRIES
        if not on_unfixed["symptom"] or (on_unfixed["broken"] and not last_try):
            feedback = (f"on the UNFIXED code {len(on_unfixed['symptom'])} case(s) failed with the bug's symptom; "
                        f"broken cases: {on_unfixed['broken'] or 'none'}; cases that passed (so they do not catch "
                        f"the bug): {on_unfixed['passed'] or 'none'}. Every case must fail there, showing the bug.")
            tries.append({"n": n, "result": feedback})
            events.log("guard_try", key=f"try#{n}", n=n, result=feedback[:300], on_unfixed=on_unfixed)
            continue
        # 2. on the FIXED code: which cases of the class the fix closed, and which it left open
        (Path(fixed) / rel).write_text(content)
        r = run_one(Path(fixed), profile, rel, run_cmd, extra=_verbose(profile))
        out_f = (r.stdout or "") + (r.stderr or "")
        (Path(keep_dir) / Path(rel).name).write_text(content)
        (Path(fixed) / rel).unlink()  # kept in the run folder, not in the fix (see the module note)
        on_fixed = judged(focus, out_f, lang)
        tries.append({"n": n, "result": "caught the bug on the unfixed code"})
        events.log("guard_try", key=f"try#{n}", n=n, result="accepted", on_unfixed=on_unfixed,
                   on_fixed={k: len(v) for k, v in on_fixed.items()})
        return {"status": "CATCHES THE BUG", "path": str(Path(keep_dir) / Path(rel).name), "repo_path": rel,
                "covers": covers, "on_unfixed": on_unfixed,
                # on the fixed code: passed = closed by this fix; symptom = still open; broken = a bad case, not a claim
                "on_fixed": {"passed": on_fixed["passed"], "failed": on_fixed["symptom"], "broken": on_fixed["broken"]},
                "broken_cases": on_unfixed["broken"], "fixed_all_green": r.returncode == 0, "tries": tries}
    return {"status": "NOT WRITTEN", "why": f"no guard caught the bug in {GUARD_TRIES} tries: {feedback}", "tries": tries}
