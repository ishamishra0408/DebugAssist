"""Free disk: delete the code copy (runs/<id>/checkout) of every run that is FINISHED, keeping everything else.

Finished = the run has no next step (stopped, rejected or published), or it was never a pipeline run (a trial).
A run paused for approval or interrupted mid-way keeps its copy: resume and the fix step need it.
Before deleting, the copy's changes (the drafted tests, any fix) are saved as runs/<id>/checkout.patch, so the copy
can be rebuilt from the base checkout plus that patch. Dry run unless yes=True.
"""
import shutil
import subprocess
from pathlib import Path

from .config import CFG


def _changes(checkout: Path) -> str:
    """Tracked edits plus every untracked file (the drafted tests), as one patch."""
    tracked = subprocess.run(["git", "-C", str(checkout), "diff"], capture_output=True, text=True).stdout
    new = subprocess.run(["git", "-C", str(checkout), "ls-files", "--others", "--exclude-standard"],
                         capture_output=True, text=True).stdout.split()
    untracked = "".join(subprocess.run(["git", "-C", str(checkout), "diff", "--no-index", "/dev/null", f],
                                       capture_output=True, text=True).stdout for f in new)
    return tracked + untracked


def _size_mb(path: Path) -> int:
    out = subprocess.run(["du", "-sm", str(path)], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else 0


def plan(is_finished, runs_dir: Path | None = None) -> list[dict]:
    rows = []
    for run in sorted((runs_dir or CFG.runs_dir).glob("*")):
        co = run / "checkout"
        if not co.is_dir():
            continue
        fin, why = is_finished(run.name)
        rows.append({"run": run.name, "checkout": co, "delete": fin, "why": why})
    return rows


def run(is_finished, yes: bool = False, runs_dir: Path | None = None) -> list[dict]:
    rows = plan(is_finished, runs_dir)
    for r in rows:
        r["apparent_mb"] = _size_mb(r["checkout"])
        if r["delete"] and yes:
            (r["checkout"].parent / "checkout.patch").write_text(_changes(r["checkout"]))
            shutil.rmtree(r["checkout"])
            r["deleted"] = True
    return rows
