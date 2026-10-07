"""test_past_bugs, the back-test half: 🎯 would-have-caught and its counter, false alarms (design: north-star-v1).
All code, no model calls.

  anchor        the commit that wrote the bug (from the second story's evidence)
  window        the anchor and the WINDOW commits before it on the main line
  at each commit, the incident's own test (the judge) says whether the bug is THERE, and the guard is run:
                 judge RED (bug there)   guard RED → caught      guard GREEN → missed
                 judge GREEN (no bug)    guard RED → FALSE ALARM guard GREEN → quiet (correct)
                 either can't run        UNEVALUABLE: never counted as a pass or a fail
  would-have-caught = the guard is RED at the anchor while the judge is RED there too.
  Design note: north-star-v1 counts false alarms "where the incident's own test is green". For #21439 the bug predates
  the anchor (#7326, 2025), so most of the window may be judge-RED: that is a finding, not a pass.

Commits that touch none of the packages the guard loads give the same answer as their neighbour, so the window is
grouped and only one commit per group is installed and run (reported as "ran r groups covering c of n commits").
Each one: checkout in a shallow history clone, install (network on) reusing the base checkout's package store,
build the guard package's dependencies (network off), run judge and guard (network off).
"""
import json
import subprocess
from pathlib import Path

from . import ladder
from .fixer import package_map, run_one
from .github_read import api
from .guard import VERBOSE, judged
from .meter import SandboxTimeExceeded

WINDOW = 50
MAX_GROUPS = 8


def _git(repo: Path, *args, timeout=600) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)


def relevant_dirs(base: Path, package_dir: str) -> set[str]:
    """The guard's package and every workspace package it depends on (dev included), transitively."""
    pmap = package_map(base)
    by_dir = {v["dir"]: k for k, v in pmap.items()}
    names, todo = set(), [by_dir.get(package_dir)]
    while todo:
        n = todo.pop()
        if n and n not in names and n in pmap:
            names.add(n)
            todo += [d for d in pmap[n]["deps"] if d in pmap]
    return {f"packages/{pmap[n]['dir']}/" for n in names}


def window(owner: str, repo: str, anchor_sha: str, n: int = WINDOW) -> list[dict]:
    """The anchor first, then the n commits before it on the main line (newest first)."""
    got = api(f"repos/{owner}/{repo}/commits?sha={anchor_sha}&per_page={n + 1}") or []
    return [{"sha": c["sha"], "date": c["commit"]["committer"]["date"][:10],
             "title": c["commit"]["message"].splitlines()[0][:90]} for c in got]


def touches(owner: str, repo: str, sha: str, dirs: set[str]) -> bool:
    files = (api(f"repos/{owner}/{repo}/commits/{sha}") or {}).get("files", [])
    return any(f["filename"].startswith(tuple(dirs)) for f in files)


def groups(commits: list[dict], touched: list[bool]) -> list[dict]:
    """Newest first. A commit that did NOT touch the guard's packages carries the code of the next OLDER commit that
    did. So a group runs from just after one touching commit down to (and including) the next one, and that touching
    commit, its oldest member, is the one to run: its code is every member's code. Commits older than the last
    touching one share the code from before the window; their oldest stands for them."""
    out, cur = [], []
    for c, t in zip(commits, touched):
        cur.append(c)
        if t:
            out.append({"rep": c, "members": cur})
            cur = []
    if cur:
        out.append({"rep": cur[-1], "members": cur})
    return out


def prepare_history(base: Path, hist: Path, repo_slug: str, anchor_sha: str, depth: int) -> None:
    if not (hist / ".git").exists():
        hist.mkdir(parents=True, exist_ok=True)
        _git(hist, "init", "-q")
        _git(hist, "remote", "add", "origin", f"https://github.com/{repo_slug}.git")
        (hist / ".git" / "info" / "exclude").write_text(".corepack/\n.pnpm-store/\n.bin/\n.da-logs/\n")
        for d in (".pnpm-store", ".corepack"):  # reuse the base's downloads (APFS copy-on-write)
            if (base / d).exists() and not (hist / d).exists():
                subprocess.run(["cp", "-cR", str(base / d), str(hist / d)], check=True, timeout=900)
    r = _git(hist, "fetch", "-q", f"--depth={depth}", "origin", anchor_sha, timeout=1200)
    if r.returncode != 0:
        raise RuntimeError(f"could not fetch history: {r.stderr[-400:]}")


def run_at(hist: Path, sha: str, profile, package_dir: str, files: dict, focus: str, run_cmd) -> dict:
    """Checkout, install, build deps, run each test in `files` ({role: (repo_path, content)})."""
    r = _git(hist, "checkout", "-q", "-f", sha)
    if r.returncode != 0:
        return {"state": "UNEVALUABLE", "why": f"checkout failed: {r.stderr[-200:]}"}
    pj = hist / "packages" / package_dir / "package.json"
    if not pj.exists():
        return {"state": "UNEVALUABLE", "why": f"packages/{package_dir} did not exist yet"}
    name = json.loads(pj.read_text())["name"]
    try:
        inst = run_cmd(profile.env + f"pnpm install --frozen-lockfile --store-dir /work/.pnpm-store --filter '{name}...'"
                       " > /work/.da-logs/bt-install.txt 2>&1", hist, network=True, timeout=900, image=profile.image)
        if inst.returncode != 0:
            return {"state": "UNEVALUABLE", "why": "install failed at this commit"}
        if profile.build_cmd:
            b = run_cmd(profile.env + f"pnpm --filter '{name}^...' build > /work/.da-logs/bt-build.txt 2>&1", hist,
                        network=False, timeout=900, image=profile.image)
            if b.returncode != 0:
                return {"state": "UNEVALUABLE", "why": "the guard package's dependencies did not build at this commit"}
        out = {}
        for role, (rel, content) in files.items():
            (hist / rel).write_text(content)
            try:
                t = run_one(hist, profile, rel, run_cmd, extra=VERBOSE.get(profile.language, ""))
            finally:
                (hist / rel).unlink()
            text = (t.stdout or "") + (t.stderr or "")
            j = judged(focus, text)
            outcome, _ = ladder.classify(profile.language, t.returncode, text)
            if t.returncode == 0:
                out[role] = "GREEN"
            elif j["symptom"]:
                out[role] = "RED"
            else:
                out[role] = "UNEVALUABLE"  # failed, but not with the bug's symptom: it can't speak about this commit
            out[role + "_cases"] = {k: len(v) for k, v in j.items()}
    except SandboxTimeExceeded as e:
        return {"state": "UNEVALUABLE", "why": f"sandbox time cap: {e}"}
    if "UNEVALUABLE" in (out.get("judge"), out.get("guard")):
        return {"state": "UNEVALUABLE", "why": "the judge or the guard could not run on this commit's code", **out}
    state = {("RED", "RED"): "CAUGHT", ("RED", "GREEN"): "MISSED", ("GREEN", "RED"): "FALSE ALARM",
             ("GREEN", "GREEN"): "QUIET"}[(out["judge"], out["guard"])]
    return {"state": state, **out}


def backtest(issue: dict, profile, base: Path, hist: Path, package_dir: str, judge: tuple, guard_file: tuple,
             anchor_sha: str, focus: str, run_cmd=None, n: int = WINDOW, max_groups: int = MAX_GROUPS) -> dict:
    from .sandbox import run_in_sandbox
    run_cmd = run_cmd or run_in_sandbox
    owner, repo = issue["owner"], issue["repo"]
    commits = window(owner, repo, anchor_sha, n)
    if not commits:
        return {"would_have_caught": None, "why": "could not list the commits before the anchor"}
    dirs = relevant_dirs(base, package_dir)
    touched = [True] + [touches(owner, repo, c["sha"], dirs) for c in commits[1:]]
    grouped = groups(commits, touched)
    prepare_history(base, hist, f"{owner}/{repo}", anchor_sha, depth=n + 2)
    files = {"judge": judge, "guard": guard_file}
    results = []
    for g in grouped[:max_groups]:
        rep = g["rep"]
        res = run_at(hist, rep["sha"], profile, package_dir, files, focus, run_cmd)
        results.append({"commit": rep["sha"][:10], "date": rep["date"], "title": rep["title"],
                        "covers": len(g["members"]), **res})
    at_anchor = results[0]
    before = results[1:]
    covered = sum(r["covers"] for r in before)
    count = lambda s: sum(r["covers"] for r in before if r["state"] == s)
    return {
        "anchor": {"sha": anchor_sha[:10], **{k: at_anchor[k] for k in at_anchor if k in ("state", "why", "judge", "guard")}},
        "would_have_caught": True if at_anchor["state"] == "CAUGHT" else (None if at_anchor["state"] == "UNEVALUABLE" else False),
        "false_alarms": {"window": n, "commits_listed": len(commits) - 1, "groups_run": len(before),
                         "commits_covered": covered, "fired": count("FALSE ALARM"), "quiet": count("QUIET"),
                         "bug_already_there": count("CAUGHT") + count("MISSED"), "missed": count("MISSED"),
                         "unevaluable": count("UNEVALUABLE"),
                         "not_run": sum(len(g["members"]) for g in grouped[max_groups:])},
        "relevant_packages": sorted(dirs),
        "groups": results,
    }
