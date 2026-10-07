"""find_cause and write_fix: from a RED test to a validated fix, the way the by-hand run of vercel/ai #21439 did it.

find_cause  The model reads the failing test, what it printed, and the located source. It may ask for a definition
            (NEED_DEFINITION: StreamingToolCallTracker); code finds and shows it. It must name a real, non-test source
            file and line range, or the run stops CAUSE NOT FOUND.
write_fix   The model answers with exact SEARCH/REPLACE edits. Code applies them only to source files (never a test, a
            fixture or the failing test itself), rebuilds what changed, then VALIDATES: the failing test must pass and
            every affected package's whole suite must stay green. Otherwise the edits are reverted and the evidence fed
            back, up to FIX_ATTEMPTS times; then the run stops FIX NOT VALIDATED. No unproven fix reaches the PR.
"""
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import events, ladder
from .models import write

FIX_ATTEMPTS = 3
CAUSE_LOOKUPS = 2


class FixRefused(ValueError):
    pass


def _git(checkout: Path, *args) -> str:
    return subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, text=True, timeout=120).stdout


def is_test_path(path: str) -> bool:
    return bool(re.search(r"\.test\.[tj]sx?$|__fixtures__/|__snapshots__/|/da-repro-|(^|/)tests?/|test_[^/]+\.py$", path))


# ── definitions on request ───────────────────────────────────────────────────────────────────────
def find_definition(checkout: Path, name: str, max_lines: int = 260) -> str:
    """Where `name` is defined (class / function / const / type), with the code from there on."""
    if not re.fullmatch(r"[A-Za-z_$][\w$]{2,80}", name):
        return f"(not looked up: {name!r} is not an identifier)"
    hits = _git(checkout, "grep", "-n", "-E", rf"(class|function|const|let|interface|type|enum)\s+{name}\b",
                "--", "packages/*/src/**", ":!*.test.ts", ":!*.test.tsx").splitlines()
    if not hits:
        return f"(no definition of {name} found in packages/*/src)"
    path, num, _ = hits[0].split(":", 2)
    lines = (checkout / path).read_text().splitlines()
    start = max(1, int(num) - 3)
    body = "\n".join(f"{n:5d}  {lines[n - 1]}" for n in range(start, min(len(lines), start + max_lines) + 1))
    return f"--- {path} (definition of {name} at line {num})\n{body}"


# ── find_cause ───────────────────────────────────────────────────────────────────────────────────
CAUSE_SYSTEM = """You find the cause of a bug that a failing test has already reproduced.
You may first ask to see definitions, one per line:  NEED_DEFINITION: <identifier>   (at most 3 per reply)
When the code shown calls into another module (a helper, a tracker, a shared class), look up that definition before
deciding: the cause is often in the shared code, not at the call site.
When you know the cause, reply exactly:
CAUSE_FILE: <repo-relative path of the source file that must change>
CAUSE_LINES: <start>-<end>
WHY: <2-4 sentences: what the code does wrong, and why the failing test shows it>
FIX_PLAN: <1-3 sentences: the smallest change that makes the test pass without breaking other callers>"""


def cause_messages(focus: str, test_path: str, test_code: str, evidence: str, snippets: str, source: str,
                   looked_up: list[str]) -> list:
    extra = "\n\n".join(looked_up)
    user = f"""FOCUS (the problem reproduced): {focus}

FAILING TEST ({test_path}):
{test_code[:6000]}

WHAT IT PRINTED ON THE CURRENT CODE:
{evidence[:2500]}

MOST RELEVANT SOURCE: {source}
{snippets}
{('DEFINITIONS YOU ASKED FOR:' + chr(10) + extra) if extra else ''}"""
    return [("system", CAUSE_SYSTEM), ("user", user)]


def parse_cause(reply: str, checkout: Path) -> dict:
    f = re.search(r"CAUSE_FILE:\s*`?([^\s`]+)`?", reply)
    l = re.search(r"CAUSE_LINES:\s*(\d+)\s*[-–]\s*(\d+)", reply)
    why = re.search(r"WHY:\s*(.+?)(?=\nFIX_PLAN:|\Z)", reply, re.S)
    plan = re.search(r"FIX_PLAN:\s*(.+)", reply, re.S)
    if not (f and l and why):
        raise FixRefused("the reply has no CAUSE_FILE / CAUSE_LINES / WHY")
    path, a, b = f.group(1), int(l.group(1)), int(l.group(2))
    if is_test_path(path):
        raise FixRefused(f"the cause must be in source, not a test: {path}")
    if not (checkout / path).is_file():
        raise FixRefused(f"no such file: {path}")
    n = len((checkout / path).read_text().splitlines())
    if not (1 <= a <= b <= n):
        raise FixRefused(f"lines {a}-{b} are outside {path} (1-{n})")
    return {"file": path, "lines": [a, b], "why": why.group(1).strip(), "plan": (plan.group(1).strip() if plan else "")}


def find_cause(state: dict, checkout: Path, ctx, test_path: str, evidence: str) -> dict:
    """Up to CAUSE_LOOKUPS rounds of definitions, then one answer; one retry if the answer breaks a rule."""
    test_code = (checkout / test_path).read_text()
    focus = state.get("focus") or state["issue"]["title"]
    looked_up, asked, refusal = [], [], ""
    for _ in range(CAUSE_LOOKUPS + 2):
        msgs = cause_messages(focus, test_path, test_code, evidence, ctx.snippets, ctx.source, looked_up)
        if refusal:
            msgs[-1] = (msgs[-1][0], msgs[-1][1] + f"\n\nYOUR LAST ANSWER WAS REFUSED: {refusal}. Answer again.")
        msg, _ = write(state, "find_cause", msgs, max_tokens=1500)
        reply = str(msg.content)
        wanted = [w for w in re.findall(r"NEED_DEFINITION:\s*`?([\w$]+)`?", reply) if w not in asked][:3]
        if wanted and len(asked) < 3 * CAUSE_LOOKUPS and "CAUSE_FILE:" not in reply:
            asked += wanted
            looked_up += [find_definition(checkout, w) for w in wanted]
            continue
        try:
            cause = parse_cause(reply, checkout)
            cause["looked_up"] = asked
            return cause
        except FixRefused as e:
            if refusal:
                raise
            refusal = str(e)
    raise FixRefused("no valid cause after the allowed rounds")


# ── write_fix ────────────────────────────────────────────────────────────────────────────────────
FIX_SYSTEM = """You fix a bug that a failing test reproduces, with the smallest correct change.
If you must see code you were not shown before editing it, reply ONLY with lines  NEED_DEFINITION: <identifier>
(at most 3); you will get the code and be asked again. Otherwise answer ONLY with edit blocks, as many as needed:
FILE: <repo-relative path>
<<<<<<< SEARCH
<exact lines from the file, copied character for character, enough to match ONE place>
=======
<the replacement lines>
>>>>>>> REPLACE
Rules:
- Change source files only: never a test file, a fixture, or the failing test. The failing test is the judge.
- Keep behaviour for every other caller unless the bug itself requires a change (prefer an optional, defaulted
  parameter over changing a shared function's meaning).
- Afterwards the failing test must pass AND every existing test in the affected packages must still pass."""


@dataclass
class Edit:
    path: str
    search: str
    replace: str


_BLOCK = re.compile(r"<<<<<<< SEARCH[ \t]*\n(.*?)\n=======[ \t]*\n(.*?)\n?>>>>>>> REPLACE", re.S)
_PATH = re.compile(r"^\s*(?:FILE:\s*`?([\w./@-]+\.\w+)`?|`?((?:[\w.@-]+/)+[\w.@-]+\.\w+)`?)\s*:?\s*$")


def parse_edits(reply: str) -> list[Edit]:
    """Each SEARCH/REPLACE block, with the file named on the nearest line above it ("FILE: path", or a bare path,
    inside or outside a ``` fence). Dev-model run 2026-10-07: right edits, path written inside a fence, all refused."""
    edits, last_path = [], None
    pos = 0
    for m in _BLOCK.finditer(reply):
        for line in reversed(reply[pos:m.start()].splitlines()):
            if line.strip().startswith("```") or not line.strip():
                continue
            p = _PATH.match(line)
            if p:
                last_path = p.group(1) or p.group(2)
            break
        if not last_path:
            raise FixRefused("an edit block names no file (put the path on the line above <<<<<<< SEARCH)")
        edits.append(Edit(last_path, m.group(1), m.group(2)))
        pos = m.end()
    if not edits:
        raise FixRefused("the reply has no SEARCH / REPLACE block")
    return edits


def apply_edits(checkout: Path, edits: list[Edit]) -> list[str]:
    """All-or-nothing: every edit is checked before any file is written."""
    staged = {}
    for e in edits:
        if is_test_path(e.path):
            raise FixRefused(f"a fix may not edit tests or fixtures: {e.path}")
        if not re.match(r"packages/[^/]+/src/", e.path):
            raise FixRefused(f"a fix edits package source only: {e.path}")
        f = checkout / e.path
        if not f.is_file():
            raise FixRefused(f"no such file: {e.path}")
        text = staged.get(e.path, f.read_text())
        count = text.count(e.search)
        if count != 1:
            raise FixRefused(f"SEARCH matches {count} places in {e.path}; it must match exactly one")
        staged[e.path] = text.replace(e.search, e.replace, 1)
    for path, text in staged.items():
        (checkout / path).write_text(text)
    return sorted(staged)


def revert(checkout: Path, paths: list[str]) -> None:
    if paths:
        _git(checkout, "checkout", "--", *paths)


# which packages a change touches: the changed ones and every installed package that depends on them
def package_map(checkout: Path) -> dict:
    out = {}
    for pj in (checkout / "packages").glob("*/package.json"):
        d = json.loads(pj.read_text())
        deps = set()
        for k in ("dependencies", "devDependencies", "peerDependencies"):
            deps |= set((d.get(k) or {}).keys())
        out[d["name"]] = {"dir": pj.parent.name, "deps": deps}
    return out


def affected(checkout: Path, changed_files: list[str], suite_names: list[str]) -> tuple[list[str], list[str]]:
    """(changed package names, package dirs whose suites must stay green: changed + dependents among the installed)."""
    pmap = package_map(checkout)
    by_dir = {v["dir"]: k for k, v in pmap.items()}
    changed = sorted({by_dir[p.split("/")[1]] for p in changed_files if p.split("/")[1] in by_dir})
    hit = set(changed)
    grew = True
    while grew:
        grew = False
        for name in suite_names:
            if name not in hit and pmap.get(name, {}).get("deps", set()) & hit:
                hit.add(name)
                grew = True
    return changed, sorted(pmap[n]["dir"] for n in hit if n in pmap and (n in suite_names or n in changed))


def suite_names(profile) -> list[str]:
    return re.findall(r"--filter '([^']+?)\.\.\.'", profile.filters or "")


def fix_messages(focus: str, cause: dict, cause_file_text: str, test_path: str, test_code: str, evidence: str,
                 history: list[str], looked_up: list[str]) -> list:
    lines = cause_file_text.splitlines()
    a, b = cause["lines"]
    if len(lines) > 700:
        lo, hi = max(1, a - 150), min(len(lines), b + 150)
        shown = "\n".join(lines[lo - 1:hi])
        where = f"lines {lo}-{hi} of {len(lines)}"
    else:
        shown, where = cause_file_text, "whole file"
    past = "".join(f"\n- {h}" for h in history) or " none"
    user = f"""FOCUS: {focus}

CAUSE: {cause['file']} lines {a}-{b}
WHY: {cause['why']}
PLAN: {cause['plan']}

{cause['file']} ({where}):
{shown}

FAILING TEST ({test_path}), which must pass after your change:
{test_code[:5000]}

IT PRINTS NOW:
{evidence[:1500]}
{('OTHER CODE (definitions looked up):' + chr(10) + chr(10).join(looked_up)[:14000]) if looked_up else ''}
EARLIER FIX ATTEMPTS (reverted):{past}"""
    return [("system", FIX_SYSTEM), ("user", user)]


def validate(checkout: Path, profile, test_path: str, built: set, changed_files: list[str], run_cmd) -> dict:
    """Rebuild every package ever changed (so a reverted attempt leaves no stale build), run the failing test, then the
    whole suite of each affected package. Returns {"ok", "red_to_green", "suites", "evidence"}."""
    changed, suites = affected(checkout, changed_files, suite_names(profile))
    built |= set(changed)
    out = {"ok": False, "red_to_green": False, "suites": {}, "changed_packages": changed, "evidence": ""}
    if profile.build_cmd and built:
        flt = " ".join(f"--filter '{n}'" for n in sorted(built))
        r = run_cmd(profile.env + f"pnpm {flt} build", checkout, network=False, timeout=900, image=profile.image)
        if r.returncode != 0:
            out["evidence"] = "build failed: " + (r.stdout + r.stderr)[-1500:]
            return out
    pkg = test_path.split("/")[1]
    r = run_cmd(profile.env + profile.test_cmd.format(package=pkg, test_path=str(Path(test_path).relative_to(f"packages/{pkg}"))),
                checkout, network=False, timeout=300, image=profile.image)
    outcome, line = ladder.classify(profile.language, r.returncode, (r.stdout or "") + (r.stderr or ""))
    if outcome != ladder.GREEN:
        out["evidence"] = f"the failing test is still {outcome}: {line}\n" + _assertion(r)
        return out
    out["red_to_green"] = True
    for d in suites:
        r = run_cmd(profile.env + profile.test_cmd.format(package=d, test_path=""), checkout, network=False,
                    timeout=900, image=profile.image)
        out["suites"][d] = "pass" if r.returncode == 0 else "FAIL"
        if r.returncode != 0:
            out["evidence"] = f"the fix breaks the {d} suite:\n" + _assertion(r)
            return out
    out["ok"] = True
    return out


def _assertion(r) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", (r.stdout or "") + (r.stderr or ""))
    m = re.search(r"(FAIL .*\n(?:.*\n){0,3})?(AssertionError|Error|TypeError)[^\n]*\n(?:.*\n){0,10}", text)
    return (m.group(0) if m else text[-1200:]).strip()[:1500]


def write_fix(state: dict, checkout: Path, cause: dict, test_path: str, evidence: str, profile, run_cmd=None,
              looked_up: list[str] | None = None, stale: list[str] | None = None, drafts: Path | None = None) -> dict:
    """stale: files a crashed attempt had changed (now reverted); their packages are rebuilt before validating."""
    from .sandbox import run_in_sandbox
    run_cmd = run_cmd or run_in_sandbox
    focus = state.get("focus") or state["issue"]["title"]
    test_code = (checkout / test_path).read_text()
    history, attempts, asked = [], [], []
    looked_up = list(looked_up or [])
    built = set(affected(checkout, stale, suite_names(profile))[0]) if stale else set()
    for n in range(1, FIX_ATTEMPTS + 1):
        for _ in range(2):  # up to 2 lookup rounds per attempt (each one model call, under the write_fix turn cap)
            msg, _ = write(state, "write_fix", fix_messages(focus, cause, (checkout / cause["file"]).read_text(),
                                                            test_path, test_code, evidence, history, looked_up),
                           max_tokens=4000)
            wanted = [w for w in re.findall(r"NEED_DEFINITION:\s*`?([\w$]+)`?", str(msg.content)) if w not in asked][:3]
            if not wanted or "<<<<<<< SEARCH" in str(msg.content):
                break
            asked += wanted
            looked_up += [find_definition(checkout, w) for w in wanted]
        if drafts:
            Path(drafts).mkdir(parents=True, exist_ok=True)
            (Path(drafts) / f"fix-{n}.md").write_text(str(msg.content))  # every raw reply, for the record
        changed: list[str] = []
        try:
            changed = apply_edits(checkout, parse_edits(str(msg.content)))
            result = validate(checkout, profile, test_path, built, changed, run_cmd)
        except FixRefused as e:
            result = {"ok": False, "red_to_green": False, "suites": {}, "evidence": f"edit refused: {e}"}
        rec = {"n": n, "ok": result["ok"], "changed": changed, "red_to_green": result["red_to_green"],
               "suites": result["suites"], "evidence": result["evidence"][:1500]}
        events.log("fix_attempt", **rec)
        attempts.append(rec)
        if result["ok"]:
            patch = _git(checkout, "diff", "--", *changed)
            return {"status": "VALIDATED", "attempts": attempts, "changed": changed, "patch": patch,
                    "red_to_green": True, "suites": result["suites"]}
        revert(checkout, changed)
        history.append(f"attempt {n}: {result['evidence'][:700]}")
    if built:  # leave the copy's builds matching its (reverted) source
        flt = " ".join(f"--filter '{n}'" for n in sorted(built))
        run_cmd(profile.env + f"pnpm {flt} build", checkout, network=False, timeout=900, image=profile.image)
    return {"status": "NOT VALIDATED", "attempts": attempts, "changed": [], "patch": "", "red_to_green": False,
            "suites": {}}
