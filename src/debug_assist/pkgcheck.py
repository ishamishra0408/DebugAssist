"""Is the package the bug lives in installed on the test machine? Asked before any test is written; when it isn't, the
run pauses and asks the person whether to install it (Isha 2026-10-10: "check if all dependencies related to this issue
are installed; if not, alert the user and ask yes / no; if yes, install it and then reproduce", after #22085: the
workflow package was not installed and the first run spent 4 tries on tests that could never load).

  missing(profile, checkout, pkg_dir)   {"dir", "name"} when the package has no node_modules on the test machine
  add(repo, name)                       the package joins the repo's install list for good (MongoDB profile_extras)
  rebuild(profile, checkout, log)       the test machine rebuilt with it: the E2B template (hosted) or this Mac's base
                                        and the run's own copy (local), then the run's sandbox starts afresh

Only for repos whose install picks packages ({filters}, e.g. vercel/ai); a repo installed whole has nothing to add.
Runs have no internet, on purpose, so "installing" means rebuilding the test machine, never installing inside a run.
"""
import json
import re
import time
from pathlib import Path

MINUTES = 3   # what a rebuild took (vercel/ai with workflow, 2026-10-09: 2 min 46 s)
_cache: dict = {}


def applies(profile) -> bool:
    return getattr(profile, "language", "") == "typescript" and "{filters}" in (getattr(profile, "install_cmd", "") or "")


def package_name(checkout: Path, pkg_dir: str) -> str:
    try:
        return str(json.loads((Path(checkout) / pkg_dir / "package.json").read_text()).get("name") or "")
    except (OSError, ValueError):
        return ""


def missing(profile, checkout: Path, pkg_dir: str, run_cmd=None, trust_list: bool = True) -> dict | None:
    """Ask the test machine itself: a package pnpm did not install has no node_modules of its own. With trust_list,
    a package on the install list is taken as installed (no sandbox started); after a rebuild it is checked anyway."""
    if not applies(profile) or not pkg_dir or pkg_dir == ".":
        return None
    name = package_name(checkout, pkg_dir)
    if not name or (trust_list and f"'{name}...'" in (getattr(profile, "filters", "") or "")):
        return None   # on the install list already
    from .sandbox import run_in_sandbox
    r = (run_cmd or run_in_sandbox)(f"test -d {pkg_dir}/node_modules && echo INSTALLED || echo MISSING", Path(checkout),
                                    network=False, timeout=60, image=profile.image)
    out = (r.stdout or "") + (r.stderr or "")
    return {"dir": pkg_dir, "name": name} if "MISSING" in out else None


def _coll():
    from .store import db
    return db()["profile_extras"]


def extras(repo: str) -> list[str]:
    """Packages a person chose to install for this repo, cached for 15 s (profiles.get asks often)."""
    hit = _cache.get(repo)
    if hit and time.monotonic() - hit[0] < 15:
        return hit[1]
    try:
        from .store import reachable
        got = list((_coll().find_one({"_id": repo}) or {}).get("packages") or []) if reachable() else []
    except Exception:
        got = []
    _cache[repo] = (time.monotonic(), got)
    return got


def with_extras(profile):
    """The profile with the chosen packages added to its install list."""
    if not applies(profile):
        return profile
    more = [n for n in extras(profile.repo) if f"'{n}...'" not in (profile.filters or "")]
    if not more:
        return profile
    import dataclasses
    return dataclasses.replace(profile, filters=(profile.filters + " " + " ".join(f"--filter '{n}...'" for n in more)).strip())


def add(repo: str, name: str) -> None:
    if not re.fullmatch(r"(@[\w.-]+/)?[\w.-]+", name or ""):
        raise ValueError(f"not a package name: {name!r}")
    _coll().update_one({"_id": repo}, {"$addToSet": {"packages": name}}, upsert=True)
    _cache.pop(repo, None)


def recipe(profile) -> str:
    """The test machine's recipe: the repo at its pinned commit, installed and built with the install list."""
    env = profile.env.rstrip().removesuffix("&&").strip()
    return (f"# Written by pkgcheck.py from the {profile.repo} profile and its install list\nFROM {profile.image}\n"
            "RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*\n"
            f"WORKDIR /work\nRUN git init -q && git remote add origin https://github.com/{profile.repo}.git \\\n"
            f" && git fetch -q --depth 1 origin {profile.base_commit} && git checkout -q FETCH_HEAD \\\n"
            " && printf '.corepack/\\n.pnpm-store/\\n.bin/\\n.da-logs/\\n' > .git/info/exclude\n"
            f"RUN {env} && {profile.install_cmd.format(filters=profile.filters)}\n"
            f"RUN {env} && {profile.build_cmd.format(filters=profile.filters)}\n"
            "RUN env | cut -d= -f1 | sort > /etc/da-env-baseline\n")


def rebuild(profile, checkout: Path, log=print) -> None:
    """Rebuild the test machine with the install list. Hosted: the E2B template under the same name, so every later
    run uses it. On this Mac: install into the prepared base and into this run's own copy (network on, once)."""
    from .config import CFG
    if CFG.sandbox_backend == "e2b":
        import tempfile
        from e2b import Template
        from .sandbox_e2b import close, template_for
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "e2b.Dockerfile"
            f.write_text(recipe(profile))
            Template.build(Template().from_dockerfile(str(f)), alias=template_for(Path(checkout)), cpu_count=2,
                           memory_mb=4096, on_build_logs=lambda entry: log(str(getattr(entry, "message", entry))))
        close(Path(checkout))   # the run's next command starts a sandbox from the rebuilt machine
        return
    from .checkout import base_path
    from .sandbox import run_in_sandbox
    env = profile.env.rstrip()
    cmd = (f"{env} {profile.install_cmd.format(filters=profile.filters)} && "
           f"{profile.build_cmd.format(filters=profile.filters)}")
    for where in (base_path(profile), Path(checkout)):
        r = run_in_sandbox(cmd, where, network=True, timeout=1800, image=profile.image)
        if r.returncode != 0:
            raise RuntimeError(f"installing failed in {where.name} (exit {r.returncode}): "
                               f"{((r.stdout or '') + (r.stderr or ''))[-400:]}")
