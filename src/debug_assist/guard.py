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

from . import ladder
from .budget import BudgetExceeded, TurnCapExceeded
from .fixer import run_one
from .models import write
from .testwriter import WriterRefused, parse, right_reason, validate

GUARD_TRIES = 2
# every case listed, passing ones too: vitest folds an all-green file into one line (Opus run 2026-10-07)
VERBOSE = {"typescript": "--reporter=verbose"}


def sibling_sites(checkout: Path, lines, fixed_file: str) -> list[str]:
    """Other source lines identical to ANY line the fix changed: the same bug, waiting elsewhere. (Opus run 2026-10-07:
    the first changed line was specific to the fixed file; the second, `toolCallTracker.flush();`, is in 6 others.)"""
    out = []
    for line in ([lines] if isinstance(lines, str) else list(lines)):
        if not line:
            continue
        got = subprocess.run(["git", "-C", str(checkout), "grep", "-n", "-F", "-e", line, "--", "packages/*/src/**",
                              ":!*.test.ts", ":!*.test.tsx"], capture_output=True, text=True, timeout=60).stdout
        out += [l.split(":", 2)[0] + ":" + l.split(":", 2)[1] for l in got.splitlines()
                if not l.startswith(fixed_file + ":")]
    return sorted(set(out))


SYSTEM = """You write a LASTING GUARD: ONE vitest test file that fails whenever this CLASS of bug exists, not only the
one case that was reported. Parametrise it with it.each over every trigger of the class that this code can meet
(for example every way a stream can end before a value is complete). Each case asserts the CORRECT behaviour and
shows the evidence on failure (compare the actual parts, not counts).
Use only the setup and helpers shown. No network, no env vars. Fixture paths are relative to the package directory.
Reply exactly:
COVERS: <one line: the class of input the cases cover>
```ts
<the whole test file>
```"""


def messages(focus: str, condition: str, cause: dict, fix_patch: str, judge_code: str, header: str,
             conditions_text: str, feedback: str, fixtures: list | None = None) -> list:
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
    return [("system", SYSTEM), ("user", user)]


def failure_blocks(output: str) -> dict:
    """vitest's ' FAIL  file > describe > case' sections: case header → what it printed."""
    clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
    parts = re.split(r"(?m)^ FAIL  ", clean)[1:]
    heads = [p.splitlines()[0] for p in parts]
    bodies = ["\n".join(p.splitlines()[1:25]) for p in parts]
    # vitest prints cases that failed with the SAME error as consecutive headers sharing one block (trial 2026-10-07:
    # the first case of a pair looked like it showed nothing and was called broken)
    for i in range(len(bodies) - 2, -1, -1):
        if not bodies[i].strip():
            bodies[i] = bodies[i + 1]
    return dict(zip(heads, bodies))


def judged(focus: str, output: str) -> dict:
    """Each case, judged on its OWN failure. Dev trial 2026-10-07: one case failed only because it read a fixture
    file that doesn't exist, and was nearly reported as a part of the bug class the fix left open."""
    got, blocks = cases(output), failure_blocks(output)
    out = {"passed": got["passed"], "symptom": [], "broken": []}
    for name in got["failed"]:
        block = next((b for h, b in blocks.items() if name.rstrip("…") in h), "")
        (out["symptom"] if right_reason(focus, block) else out["broken"]).append(
            name if right_reason(focus, block) else f"{name}: {(_first_error(block) or 'no assertion shown')[:200]}")
    return out


def _first_error(block: str) -> str:
    m = re.search(r"^\s*(\w*Error\b.*)$", block, re.M)
    return m.group(1).strip() if m else ""


def cases(output: str) -> dict:
    """Per-case results from vitest's default reporter: {"passed": [...], "failed": [...]}."""
    clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
    passed = re.findall(r"^\s*✓\s+(.+?)(?:\s+\d+ms)?$", clean, re.M)
    failed = re.findall(r"^\s*[×✗]\s+(.+?)(?:\s+\d+ms)?$", clean, re.M)
    return {"passed": passed, "failed": failed}


def write_guard(state: dict, profile, fixed: Path, unfixed: Path, judge: str, cause: dict, fix_patch: str,
                condition: str, conditions_text: str, header: str, keep_dir: Path, run_cmd=None,
                fixtures: list | None = None) -> dict:
    from .sandbox import run_in_sandbox
    run_cmd = run_cmd or run_in_sandbox
    focus = state.get("focus") or state["issue"]["title"]
    rel = str(Path(judge).with_name(f"da-guard-{state['issue']['number']}.test.ts"))
    judge_code = (Path(fixed) / judge).read_text()
    feedback, tries = "", []
    for n in range(1, GUARD_TRIES + 1):
        try:
            msg, _ = write(state, "lasting_guard", messages(focus, condition, cause, fix_patch, judge_code, header,
                                                            conditions_text, feedback, fixtures), max_tokens=4000)
        except (TurnCapExceeded, BudgetExceeded) as e:
            return {"status": "NOT WRITTEN", "why": f"stopped by a cap: {e}", "tries": tries}
        reply = str(msg.content)
        Path(keep_dir).mkdir(parents=True, exist_ok=True)
        (Path(keep_dir) / f"guard-{n}.md").write_text(reply)
        covers = (re.search(r"COVERS:\s*(.+)", reply) or [None, ""])[1].strip()
        try:
            content, _ = parse(reply)
            validate(content, ladder.Rung(0, "guard", "lasting guard"))  # no fixture rule either way
        except WriterRefused as e:
            feedback = f"refused: {e}"
            tries.append({"n": n, "result": feedback})
            continue
        # 1. on the UNFIXED code: it must catch this bug, for the focus's reason
        (Path(unfixed) / rel).write_text(content)
        r = run_one(Path(unfixed), profile, rel, run_cmd, extra=VERBOSE.get(profile.language, ""))
        out_u = (r.stdout or "") + (r.stderr or "")
        outcome, line = ladder.classify(profile.language, r.returncode, out_u)
        (Path(unfixed) / rel).unlink()
        on_unfixed = judged(focus, out_u)
        last_try = n == GUARD_TRIES
        if not on_unfixed["symptom"] or (on_unfixed["broken"] and not last_try):
            feedback = (f"on the UNFIXED code {len(on_unfixed['symptom'])} case(s) failed with the bug's symptom; "
                        f"broken cases: {on_unfixed['broken'] or 'none'}; cases that passed (so they do not catch "
                        f"the bug): {on_unfixed['passed'] or 'none'}. Every case must fail there, showing the bug.")
            tries.append({"n": n, "result": feedback})
            continue
        # 2. on the FIXED code: which cases of the class the fix closed, and which it left open
        (Path(fixed) / rel).write_text(content)
        r = run_one(Path(fixed), profile, rel, run_cmd, extra=VERBOSE.get(profile.language, ""))
        out_f = (r.stdout or "") + (r.stderr or "")
        (Path(keep_dir) / Path(rel).name).write_text(content)
        (Path(fixed) / rel).unlink()  # kept in the run folder, not in the fix (see the module note)
        on_fixed = judged(focus, out_f)
        tries.append({"n": n, "result": "caught the bug on the unfixed code"})
        return {"status": "CATCHES THE BUG", "path": str(Path(keep_dir) / Path(rel).name), "repo_path": rel,
                "covers": covers, "on_unfixed": on_unfixed,
                # on the fixed code: passed = closed by this fix; symptom = still open; broken = a bad case, not a claim
                "on_fixed": {"passed": on_fixed["passed"], "failed": on_fixed["symptom"], "broken": on_fixed["broken"]},
                "broken_cases": on_unfixed["broken"], "fixed_all_green": r.returncode == 0, "tries": tries}
    return {"status": "NOT WRITTEN", "why": f"no guard caught the bug in {GUARD_TRIES} tries: {feedback}", "tries": tries}
