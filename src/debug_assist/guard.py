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


def sibling_sites(checkout: Path, line: str, fixed_file: str) -> list[str]:
    """Other source lines identical to the one the fix changed: the same bug, waiting elsewhere."""
    if not line:
        return []
    out = subprocess.run(["git", "-C", str(checkout), "grep", "-n", "-F", "-e", line, "--", "packages/*/src/**",
                          ":!*.test.ts", ":!*.test.tsx"], capture_output=True, text=True, timeout=60).stdout
    return [l.split(":", 2)[0] + ":" + l.split(":", 2)[1] for l in out.splitlines() if not l.startswith(fixed_file + ":")]


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
             conditions_text: str, feedback: str) -> list:
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
{('YOUR LAST GUARD: ' + feedback) if feedback else ''}"""
    return [("system", SYSTEM), ("user", user)]


def cases(output: str) -> dict:
    """Per-case results from vitest's default reporter: {"passed": [...], "failed": [...]}."""
    clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
    passed = re.findall(r"^\s*✓\s+(.+?)(?:\s+\d+ms)?$", clean, re.M)
    failed = re.findall(r"^\s*[×✗]\s+(.+?)(?:\s+\d+ms)?$", clean, re.M)
    return {"passed": passed, "failed": failed}


def write_guard(state: dict, profile, fixed: Path, unfixed: Path, judge: str, cause: dict, fix_patch: str,
                condition: str, conditions_text: str, header: str, keep_dir: Path, run_cmd=None) -> dict:
    from .sandbox import run_in_sandbox
    run_cmd = run_cmd or run_in_sandbox
    focus = state.get("focus") or state["issue"]["title"]
    rel = str(Path(judge).with_name(f"da-guard-{state['issue']['number']}.test.ts"))
    judge_code = (Path(fixed) / judge).read_text()
    feedback, tries = "", []
    for n in range(1, GUARD_TRIES + 1):
        try:
            msg, _ = write(state, "lasting_guard", messages(focus, condition, cause, fix_patch, judge_code, header,
                                                            conditions_text, feedback), max_tokens=4000)
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
        r = run_one(Path(unfixed), profile, rel, run_cmd)
        out_u = (r.stdout or "") + (r.stderr or "")
        outcome, line = ladder.classify(profile.language, r.returncode, out_u)
        (Path(unfixed) / rel).unlink()
        if outcome != ladder.RED or right_reason(focus, out_u) is False:
            feedback = (f"on the UNFIXED code it was {outcome}" + ("" if outcome != ladder.RED else
                        " but not with the bug's symptom") + f": {line}. It must fail there, showing the bug.")
            tries.append({"n": n, "result": feedback})
            continue
        # 2. on the FIXED code: which cases of the class the fix closed, and which it left open
        (Path(fixed) / rel).write_text(content)
        r = run_one(Path(fixed), profile, rel, run_cmd)
        out_f = (r.stdout or "") + (r.stderr or "")
        (Path(keep_dir) / Path(rel).name).write_text(content)
        (Path(fixed) / rel).unlink()  # kept in the run folder, not in the fix (see the module note)
        on_fixed = cases(out_f)
        tries.append({"n": n, "result": "caught the bug on the unfixed code"})
        return {"status": "CATCHES THE BUG", "path": str(Path(keep_dir) / Path(rel).name), "repo_path": rel,
                "covers": covers, "on_unfixed": cases(out_u), "on_fixed": on_fixed,
                "fixed_all_green": r.returncode == 0, "tries": tries}
    return {"status": "NOT WRITTEN", "why": f"no guard caught the bug in {GUARD_TRIES} tries: {feedback}", "tries": tries}
