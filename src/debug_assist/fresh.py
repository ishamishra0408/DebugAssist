"""Every run on the latest main (Isha 2026-10-10: "fetch at the start of every run; install packages only if they
changed; then build; tell the user they are on a branch forked from main, with its time, so they know they are not
behind"). Until now every run used the pinned commit (vercel/ai as of 2026-10-07), so bugs fixed since still
"reproduced" (#22085) and fixes were written against old code.

The test machine (the E2B template, or this Mac's prepared base) stays built at some commit T with its packages
installed. A run's own copy of the code is moved to main's newest commit M:
  1. fetch     latest(profile): M and when it was fetched; move(copy, M) checks the copy out at M
  2. install   only when the lockfile differs between T and M: the test machine is rebuilt at M (hosted, it then serves
               every later run; on this Mac, the run's copy is installed with the network on)
  3. build     otherwise only the packages whose code changed between T and M, and the installed packages that depend
               on them (one package's tests use the others' built output); the command is kept with the copy
               (.git/da-build-cmd) and run in each new sandbox (hosted) or once in the copy (this Mac)
The hosted sandbox already sends a copy's own changes; it now also sends every file that differs between T and M.
"""
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

LOCKFILES = ("pnpm-lock.yaml", "package-lock.json", "yarn.lock", "uv.lock", "poetry.lock")


def _git(checkout: Path, *args, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, text=True, timeout=timeout)


def latest(profile) -> dict:
    """main's newest commit, from GitHub (read-only token), and when it was read."""
    from . import github_read
    branch = getattr(profile, "default_branch", "") or "main"
    got = github_read.api(f"repos/{profile.repo}/commits/{branch}") or {}
    sha = str(got.get("sha") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError(f"could not read the newest commit of {profile.repo}@{branch}")
    when = ((got.get("commit") or {}).get("committer") or {}).get("date", "")
    return {"commit": sha, "branch": branch, "committed_at": when,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def machine(profile) -> str:
    """The commit the hosted test machine is built at: the pinned commit, until a rebuild at a newer main moves it."""
    try:
        from .store import db, reachable
        if reachable():
            doc = db()["test_machines"].find_one({"_id": profile.repo}) or {}
            if doc.get("commit"):
                return doc["commit"]
    except Exception:
        pass
    return profile.base_commit


def record_machine(repo: str, commit: str) -> None:
    from .store import db
    db()["test_machines"].update_one({"_id": repo}, {"$set": {"commit": commit,
                                                              "built_at": datetime.now(timezone.utc).isoformat()}},
                                     upsert=True)


def have(checkout: Path, commit: str) -> None:
    """The commit's objects in the copy, fetched shallowly when missing."""
    if _git(checkout, "cat-file", "-e", f"{commit}^{{commit}}").returncode != 0:
        r = _git(checkout, "fetch", "-q", "--depth", "1", "origin", commit, timeout=900)
        if r.returncode != 0:
            raise RuntimeError(f"could not fetch {commit[:7]}: {r.stderr.strip()[:300]}")


def move(checkout: Path, commit: str, built_from: str) -> None:
    """Check the copy out at `commit`, fetched shallowly; remember what its test machine was built at."""
    head = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    if head != commit:
        have(checkout, commit)
        r = _git(checkout, "checkout", "-q", commit)
        if r.returncode != 0:
            raise RuntimeError(f"could not check out {commit[:7]}: {r.stderr.strip()[:300]}")
    (Path(checkout) / ".git" / "da-built-from").write_text(built_from)


def changed(checkout: Path, a: str, b: str) -> list[str]:
    if a == b:
        return []
    have(checkout, a)
    have(checkout, b)
    r = _git(checkout, "diff", "--name-only", a, b)
    if r.returncode != 0:
        raise RuntimeError(f"could not compare {a[:7]} and {b[:7]}: {r.stderr.strip()[:300]}")
    return [p for p in r.stdout.splitlines() if p]


def _packages(checkout: Path) -> dict:
    """name → (folder, workspace packages it uses), from the copy's package.json files."""
    out = {}
    for f in Path(checkout).glob("packages/*/package.json"):
        try:
            d = json.loads(f.read_text())
        except ValueError:
            continue
        deps = {**d.get("dependencies", {}), **d.get("devDependencies", {}), **d.get("peerDependencies", {})}
        out[d.get("name", "")] = (str(f.parent.relative_to(checkout)), [n for n, v in deps.items() if str(v).startswith("workspace:")],
                                  bool((d.get("scripts") or {}).get("build")))
    return out


def installed(profile, pkgs: dict) -> set:
    """The packages the test machine installed: the install list and everything it uses."""
    roots = re.findall(r"--filter '([^']+)\.\.\.'", getattr(profile, "filters", "") or "")
    have, todo = set(), list(roots)
    while todo:
        n = todo.pop()
        if n in have or n not in pkgs:
            continue
        have.add(n)
        todo += pkgs[n][1]
    return have


def plan(profile, checkout: Path, built_from: str, commit: str) -> dict:
    """What moving from the machine's commit to main's needs: a lockfile change (install), or packages to build."""
    files = changed(checkout, built_from, commit)
    lock = sorted({f for f in files if Path(f).name in LOCKFILES})
    if lock or not files:
        return {"files": len(files), "lock_changed": bool(lock), "build": [], "build_cmd": ""}
    from . import lang as langs
    lang, pkgs = langs.of(profile), _packages(checkout)
    by_dir = {v[0]: k for k, v in pkgs.items()}
    touched = {by_dir[lang.package_of(f)] for f in files if lang.package_of(f) in by_dir}
    have = installed(profile, pkgs) if "{filters}" in (profile.install_cmd or "") else set(pkgs)
    users = {}
    for n, (_, deps, _) in pkgs.items():
        for dep in deps:
            users.setdefault(dep, set()).add(n)
    todo, build = list(touched), set()
    while todo:   # the changed packages, then everything installed that uses them
        n = todo.pop()
        if n in build or n not in have:
            continue
        build.add(n)
        todo += list(users.get(n, ()))
    build = sorted(n for n in build if pkgs[n][2])
    return {"files": len(files), "lock_changed": False, "build": build,
            "build_cmd": lang.rebuild_command(build) if build else ""}


def note(code: dict) -> str:
    """The line the page and the PR show: which main, fetched when."""
    if not code or not code.get("commit"):
        return ""
    return f"main at {code['commit'][:7]}, fetched {code.get('fetched_at', '')[:16].replace('T', ' ')} UTC"
