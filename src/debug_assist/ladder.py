"""The reproduction ladder (ruled 2026-10-06; Uber's ladder, teardown C15, adapted for libraries).

  1 unit         the smallest test that should fail: one function, made-up inputs
  2 integration  the real code path against a RECORDED fake server (no network)
  3 end-to-end   the reporter's own repro, seeded with the context they captured (only if the issue has one)

Climbed BEFORE any fix: the first test that fails for the issue's reason becomes the oracle, and later proves the fix.
Each attempt ends one of three ways:
  RED    the test failed on the unfixed code → reproduced; stop climbing
  GREEN  the test ran and passed → this rung did not reproduce it; climb to the next rung
  ERROR  the test itself didn't run (syntax, import, compile, timeout) → retry the same rung
At most REPRO_ATTEMPT_CAP attempts across all rungs. If nothing goes RED, the run stops NEVER REPRODUCED: no fix is
written for a bug we could not see (unlike Uber, which pushes a fix after its cap).

Every attempt is recorded. On resume, attempts already made count toward the cap and are not repeated.
"""
import re
from dataclasses import asdict, dataclass, field
from typing import Callable

from .config import REPRO_ATTEMPT_CAP

RED, GREEN, ERROR = "RED", "GREEN", "ERROR"
REPRODUCED, NEVER_REPRODUCED = "REPRODUCED", "NEVER REPRODUCED"


@dataclass(frozen=True)
class Rung:
    n: int
    name: str
    what: str


RUNGS = (Rung(1, "unit", "one function, made-up inputs"),
         Rung(2, "integration", "real code path against a recorded fake server, no network"),
         Rung(3, "end_to_end", "the reporter's own repro, seeded with their captured context"))


@dataclass
class Attempt:
    rung: str
    n: int             # attempt number across the whole climb, 1-based
    outcome: str       # RED | GREEN | ERROR
    evidence: str      # the lines of test output that decided it
    test_path: str = ""
    at: str = ""


@dataclass
class Climb:
    status: str                      # REPRODUCED | NEVER REPRODUCED
    rung: str | None                 # the rung that went RED
    attempts: list = field(default_factory=list)
    skipped: dict = field(default_factory=dict)  # rung → why it was not available


def plan(has_repro_p: float, has_recorded_fixtures: bool) -> tuple[list[Rung], dict]:
    """Which rungs this issue can use, and why any were left out."""
    rungs, skipped = [], {}
    for r in RUNGS:
        if r.name == "integration" and not has_recorded_fixtures:
            skipped[r.name] = "repo profile has no recorded fake server yet"
        elif r.name == "end_to_end" and has_repro_p < 0.5:
            skipped[r.name] = f"the issue has no reporter repro (Laya has_repro p={has_repro_p:.2f})"
        else:
            rungs.append(r)
    return rungs, skipped


# What a test run's output means. Decided by exit code first, then by output; unknown failures are ERROR, never RED
# (a reproduction we can't explain is not a reproduction).
_NOT_RUN = re.compile(r"SyntaxError|Cannot find module|ERR_MODULE_NOT_FOUND|Transform failed|Failed to load url|"
                      r"No test files found|ModuleNotFoundError|ImportError while importing|error during collection|"
                      r"TS\d{4}:|TIMEOUT after", re.I)
_FAILED = re.compile(r"AssertionError|ERR_ASSERTION|expected .+ to |^\s*FAILED |\d+ failed|^not ok |✗|×", re.I | re.M)


def classify(language: str, exit_code: int, output: str) -> tuple[str, str]:
    """→ (RED | GREEN | ERROR, the evidence line)."""
    def line(rx):
        m = rx.search(output)
        if not m:
            return ""
        s = output.rfind("\n", 0, m.start()) + 1
        e = output.find("\n", m.end())
        return output[s:e if e != -1 else None].strip()[:240]

    if exit_code == 0:
        return GREEN, "exit 0: the test passed on the unfixed code"
    if exit_code == 124:
        return ERROR, "timed out"
    if language == "python" and exit_code in (2, 3, 4, 5):  # pytest: interrupted, internal, usage, no tests
        return ERROR, f"pytest exit {exit_code}: " + (line(_NOT_RUN) or "the test did not run")
    if _NOT_RUN.search(output):
        return ERROR, line(_NOT_RUN)
    if _FAILED.search(output):
        return RED, line(_FAILED)
    return ERROR, f"exit {exit_code} with no recognisable test failure (not counted as a reproduction)"


def climb(rungs: list[Rung], attempt: Callable[[Rung, int, list], Attempt], already: list | None = None,
          cap: int = REPRO_ATTEMPT_CAP, skipped: dict | None = None) -> Climb:
    """attempt(rung, n, history) writes and runs one test and returns an Attempt. `already` = attempts made before a
    crash (from the run state); they count toward the cap and decide where the climb resumes."""
    history = [a if isinstance(a, Attempt) else Attempt(**a) for a in (already or [])]
    done = Climb(status=NEVER_REPRODUCED, rung=None, attempts=history, skipped=dict(skipped or {}))
    for a in history:  # a RED before the crash still counts
        if a.outcome == RED:
            done.status, done.rung = REPRODUCED, a.rung
            return done
    i = 0
    for a in history:  # resume where the record left off: past every rung that already went GREEN
        if a.outcome == GREEN:
            i = max(i, next((k + 1 for k, r in enumerate(rungs) if r.name == a.rung), i))
    while i < len(rungs) and len(history) < cap:
        a = attempt(rungs[i], len(history) + 1, list(history))
        history.append(a)
        if a.outcome == RED:
            done.status, done.rung = REPRODUCED, a.rung
            break
        if a.outcome == GREEN:
            i += 1  # this rung can't see it; climb
        # ERROR: retry the same rung (the writer gets the error back via history)
    done.attempts = history
    return done


def as_records(attempts: list) -> list[dict]:
    return [asdict(a) if isinstance(a, Attempt) else a for a in attempts]
