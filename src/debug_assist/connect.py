"""Connect a repo: read it, work out how it installs and tests, build its E2B template, prove its tests run, save it.
After this, issues from the repo can be run like vercel/ai's (profiles.get finds it in MongoDB's `repos`).

  1 read     GitHub: default branch, its latest commit, the main language; a source-only copy at that commit
  2 detect   language, package manager, test runner, where packages, source and tests live, with the evidence for each
  3 draft    install, build and test commands, and the template recipe (a Dockerfile)
  4 build    the E2B template: install and build run once, with the network on, on E2B's builders
  5 prove    the biggest packages' tests run with the network off in a sandbox from the new template: the baseline
  6 save     the profile, marked connected

No AI: every setting comes from the repo's own files (package.json, lock files, pyproject.toml, tests folders).
Progress is kept in MongoDB's `connects` collection, one document per repo, so the Connect page shows it live.
JavaScript/TypeScript (npm, pnpm, yarn; vitest, jest), plain Node (test files run with `node <file>`, node:test or
scripts that exit 1 on failure) and Python (uv, poetry, pip; pytest). Public repos only: the template fetches the code
without credentials.
"""
import json
import re
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from . import github_read
from .profiles import RepoProfile, to_doc

STEPS = [("read", "Read the repo"), ("detect", "Work out its setup"), ("draft", "Write its setup"),
         ("build", "Build its test sandbox"), ("prove", "Run its tests"), ("save", "Save it")]
SKIP_DIRS = {"node_modules", ".git", "examples", "example", "docs", "doc", "website", "fixtures", "templates",
             "template", "cookbook", "benchmarks", "e2e", ".github", "dist", "build", "site"}
MAX_PACKAGES = 30          # Python packages installed one by one into the template (a uv workspace installs all at once)
PROVE_PACKAGES = 4         # packages whose tests run at connection: the ones with the most test files
PROVE_FILES = 30          # plain Node: test files run one by one at connection
PROVE_TIMEOUT_S = 600
NODE_DEFAULT, PY_DEFAULT = 22, "3.12"
REPO = re.compile(r"^(?:https://github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_repo(text: str) -> str:
    m = REPO.match((text or "").strip())
    if not m:
        raise ValueError("That is not a GitHub repository. It should look like https://github.com/owner/repo")
    return f"{m.group(1)}/{m.group(2)}"


# ── 2 detect ─────────────────────────────────────────────────────────────────────────────────────
def _json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _toml(p: Path) -> dict:
    import tomllib
    try:
        return tomllib.loads(p.read_text())
    except Exception:
        return {}


def _skipped(rel: str) -> bool:
    return bool(set(Path(rel).parts) & SKIP_DIRS)


def _expand(root: Path, globs: list[str], marker: str) -> list[str]:
    """Package folders matching workspace globs that hold `marker` (package.json / pyproject.toml)."""
    out = []
    for g in globs:
        g = g.strip().rstrip("/")
        if not g or g.startswith("!"):
            continue
        for d in ([root] if g == "." else sorted(root.glob(g))):
            rel = d.relative_to(root).as_posix()
            if d.is_dir() and (d / marker).exists() and not _skipped(rel) and rel not in out:
                out.append(rel)
    return out


def _test_files(root: Path, pkg_dir: str, patterns: list[str], cap: int = 400) -> list[Path]:
    out = []
    for pat in patterns:
        for f in (root / pkg_dir).rglob(pat):
            if not _skipped(f.relative_to(root).as_posix()):
                out.append(f)
                if len(out) >= cap:
                    return out
    return out


def _node_major(pkg: dict, root: Path) -> tuple[int, str]:
    """.nvmrc / .node-version first; then engines.node (at least its minimum, 22 when that is lower); else 22."""
    for f in (".nvmrc", ".node-version"):
        m = re.search(r"(\d{2})", (root / f).read_text()) if (root / f).exists() else None
        if m and 18 <= int(m.group(1)) <= 24:
            return int(m.group(1)), f
    spec = str((pkg.get("engines") or {}).get("node", ""))
    majors = [int(x) for x in re.findall(r"(\d{2})", spec) if 18 <= int(x) <= 24]
    if majors:
        pick = NODE_DEFAULT if NODE_DEFAULT in majors else max(NODE_DEFAULT, min(majors)) if ">" in spec else max(majors)
        return pick, f"engines.node {spec}"
    return NODE_DEFAULT, "default"


NODE_TESTS = ("*.test.mjs", "*.test.js", "*.test.cjs", "*.spec.mjs", "*.spec.js")
CODE = ("*.js", "*.mjs", "*.cjs", "*.ts")
NOT_SOURCE = {"tests", "test", "__tests__", "docs", "site", "website", "examples", "evidence", "diagrams", "eval",
              "architecture", "node_modules", ".github"}


def detect_node_scripts(root: Path, pkg: dict) -> dict | None:
    """No vitest or jest: test files that run with plain `node <file>` (node:test, or a script that exits 1 when
    something fails). The whole repo is one package; the suite is the test files (narrowed to those that pass at
    connection); every folder with its own package.json and dependencies is installed."""
    tests = sorted({f.relative_to(root).as_posix() for pat in NODE_TESTS for f in root.rglob(pat)
                    if not _skipped(f.relative_to(root).as_posix())})
    if not tests:
        return None
    ev = [f"no vitest or jest: {len(tests)} test files run with plain node (e.g. {', '.join(tests[:3])})"]
    installs = []
    for f in sorted(root.glob("**/package.json")):
        rel = f.parent.relative_to(root).as_posix()
        if _skipped(f.relative_to(root).as_posix()):
            continue
        d = _json(f)
        if any(d.get(k) for k in ("dependencies", "devDependencies", "optionalDependencies")):
            installs.append((rel, (f.parent / "package-lock.json").exists()))
    ev.append("installs: " + (", ".join(f"{d} ({'npm ci' if lock else 'npm install'})" for d, lock in installs)
                              or "nothing (no folder has dependencies)"))
    code_dirs = sorted({Path(f.relative_to(root)).parts[0] for pat in CODE for f in root.glob(f"*/**/{pat}")
                        if not _skipped(f.relative_to(root).as_posix()) and Path(f.relative_to(root)).parts[0] not in NOT_SOURCE
                        and not re.search(r"\.(test|spec)\.", f.name)})
    ev.append("source: " + (", ".join(code_dirs) or "the whole repo"))
    style = "tests" if sum(bool({"tests", "test", "__tests__"} & set(Path(t).parts)) for t in tests) * 2 >= len(tests) else "beside"
    suffix = max((sfx for sfx in (".test.mjs", ".test.js", ".test.cjs", ".spec.mjs", ".spec.js")),
                 key=lambda sfx: sum(t.endswith(sfx) for t in tests))
    major, why = _node_major(pkg, root)
    ev.append(f"Node {major} ({why})")
    return {"language": "javascript", "manager": "npm", "runner": "node", "packages": ["."], "package_globs": (".",),
            "source_globs": tuple(f"{d}/**" for d in code_dirs) or ("**",), "test_style": style, "test_suffix": suffix,
            "node": major, "lock": False, "root_build": False, "package_manager_field": "", "installs": installs,
            "test_files": tests, "evidence": ev}


def detect_js(root: Path) -> dict | None:
    if not (root / "package.json").exists():
        return detect_node_scripts(root, {})
    pkg, ev = _json(root / "package.json"), []
    manager = "pnpm" if (root / "pnpm-lock.yaml").exists() else "yarn" if (root / "yarn.lock").exists() else "npm"
    lock = {"pnpm": "pnpm-lock.yaml", "yarn": "yarn.lock", "npm": "package-lock.json"}[manager]
    ev.append(f"package manager {manager}" + (f" ({lock})" if (root / lock).exists() else " (no lock file)"))
    globs = []
    if (root / "pnpm-workspace.yaml").exists():
        import yaml
        globs = list((yaml.safe_load((root / "pnpm-workspace.yaml").read_text()) or {}).get("packages") or [])
        ev.append(f"packages from pnpm-workspace.yaml: {', '.join(globs)}")
    elif pkg.get("workspaces"):
        ws = pkg["workspaces"]
        globs = list(ws if isinstance(ws, list) else ws.get("packages") or [])
        ev.append(f"packages from package.json workspaces: {', '.join(globs)}")
    packages = (_expand(root, globs, "package.json") if globs else []) or ["."]
    deps = {}
    for p in ["."] + packages[:80]:
        d = _json(root / p / "package.json")
        for k in ("dependencies", "devDependencies"):
            deps.update(d.get(k) or {})
    if "vitest" not in deps and "jest" not in deps:
        node = detect_node_scripts(root, pkg)
        if node:
            return node
    runner = "vitest" if "vitest" in deps else "jest" if "jest" in deps else "vitest"
    ev.append(f"test runner {runner}" + ("" if runner in deps else " (not in the dependencies; assumed)"))
    ts = (root / "tsconfig.json").exists() or any((root / p / "tsconfig.json").exists() for p in packages[:50])
    ext = "ts" if ts else "js"
    pkg_globs = tuple(sorted({"." if p == "." else (Path(p).parent / "*").as_posix() for p in packages}))
    with_src = sum((root / p / "src").is_dir() for p in packages) * 2 >= len(packages)
    source_globs = tuple(f"{g}/src/**" if with_src else f"{g}/**" for g in pkg_globs if g != ".") or \
        (("src/**",) if (root / "src").is_dir() else ("**",))
    tests = [f for p in packages[:40] for f in _test_files(root, p, [f"*.test.{ext}", f"*.spec.{ext}", f"*.test.{ext}x"], 50)]
    beside = sum(1 for f in tests if not ({"test", "tests", "__tests__"} & set(f.parts)))
    suffix = f".spec.{ext}" if sum(".spec." in f.name for f in tests) * 2 > len(tests) else f".test.{ext}"
    style = "beside" if beside * 2 >= max(1, len(tests)) else "tests"
    ev.append(f"{len(tests)} test files sampled, {beside} beside their source: new tests go {style}, named *{suffix}")
    major, why = _node_major(pkg, root)
    ev.append(f"Node {major} ({why})")
    return {"language": "typescript" if ts else "javascript", "manager": manager, "runner": runner,
            "packages": packages, "package_globs": pkg_globs, "source_globs": source_globs, "test_style": style,
            "test_suffix": suffix, "node": major, "lock": (root / lock).exists(), "root_build": "build" in (pkg.get("scripts") or {}),
            "package_manager_field": pkg.get("packageManager", ""), "evidence": ev}


def detect_py(root: Path) -> dict | None:
    ev = []
    members = list((((_toml(root / "pyproject.toml").get("tool") or {}).get("uv") or {}).get("workspace") or {})
                   .get("members") or [])
    roots = _expand(root, members or [".", "*", "src/*", "libs/*", "libs/*/*", "packages/*"], "pyproject.toml")
    if not roots and (root / "setup.py").exists():
        roots = ["."]
    if not roots:
        return None
    if len(roots) > 1 and "." in roots and not (root / "tests").is_dir():
        roots.remove(".")  # a root pyproject that only ties the packages together
    ev.append(f"{len(roots)} Python package(s): {', '.join(roots[:8])}" + (" …" if len(roots) > 8 else ""))
    workspace = bool(members)
    first = root / roots[0]
    has = lambda name: (first / name).exists() or (root / name).exists()  # noqa: E731
    manager = "uv" if has("uv.lock") else "poetry" if has("poetry.lock") else "pip"
    ev.append(f"package manager {manager}" + (" (a uv workspace: installed once, at the top)" if workspace else ""))
    if not workspace and len(roots) > MAX_PACKAGES:
        ev.append(f"only the first {MAX_PACKAGES} packages are installed in the test sandbox")
    text = (first / "pyproject.toml").read_text() if (first / "pyproject.toml").exists() else ""
    req = re.search(r'requires-python\s*=\s*"([^"]+)"', text)
    ver = "3.11" if req and re.search(r"<\s*3\.12\b", req.group(1)) else PY_DEFAULT
    ev.append(f"Python {ver}" + (f" (requires-python {req.group(1)})" if req else " (default)"))
    style = "tests/unit_tests" if any((root / r / "tests" / "unit_tests").is_dir() for r in roots) else "tests"
    ev.append(f"new tests go in the package's {style}/ folder, named test_*.py")
    return {"language": "python", "manager": manager, "runner": "pytest", "packages": roots, "workspace": workspace,
            "package_globs": tuple(roots), "source_globs": tuple("**" if r == "." else f"{r}/**" for r in roots),
            "test_style": style, "test_suffix": "test_*.py", "python": ver, "evidence": ev}


def detect(root: Path, primary: str = "") -> dict:
    """Both detectors; when a repo has both (a Python service with a JS frontend), GitHub's main language decides."""
    js, py = detect_js(root), detect_py(root)
    pick = (py if primary.lower() == "python" else js) if js and py else js or py
    if not pick:
        deeper = sorted(str(f.parent.relative_to(root)) for f in root.glob("**/package.json")
                        if not _skipped(f.relative_to(root).as_posix()))[:8]
        if deeper:
            raise ValueError("there is no package.json at the top of the repo, so there is no one way to install and test "
                             f"it (found one in: {', '.join(deeper)})")
        raise ValueError("no package.json, pyproject.toml or setup.py: not a JavaScript, TypeScript or Python repo")
    if js and py:
        pick["evidence"].insert(0, f"both JavaScript and Python found; GitHub says {primary or 'nothing'}, so {pick['language']}")
    return pick


# ── 3 draft ──────────────────────────────────────────────────────────────────────────────────────
_COREPACK = ("export COREPACK_HOME=/work/.corepack COREPACK_ENABLE_DOWNLOAD_PROMPT=0 CI=1 NO_COLOR=1 PATH=/work/.bin:$PATH"
             "{extra} && mkdir -p /work/.bin && corepack enable --install-directory /work/.bin {tool} && ")
JS_ENV = {"pnpm": _COREPACK.format(extra=" pnpm_config_verify_deps_before_run=error", tool="pnpm"),
          "yarn": _COREPACK.format(extra="", tool="yarn"),
          "npm": "export CI=1 NO_COLOR=1 && "}
PY_INSTALL = {"uv": "uv sync --group test || uv sync --all-groups || uv sync",
              "poetry": "poetry install --no-interaction --with test || poetry install --no-interaction",
              "pip": "pip install -q -e '.[test]' || pip install -q -e '.[dev]' || pip install -q -e ."}
PY_TEST = {"uv": "uv run --no-sync pytest -q {test_path}", "poetry": "poetry run pytest -q {test_path}",
           "pip": "python -m pytest -q {test_path}"}


def template_alias(repo: str, commit: str) -> str:
    return f"debugassist-{re.sub(r'[^a-z0-9]+', '-', repo.lower()).strip('-')}-{commit[:7]}"[:60]


def draft(repo: str, facts: dict, commit: str, branch: str) -> RepoProfile:
    common = dict(base_commit=commit, e2b_template=template_alias(repo, commit), default_branch=branch,
                  manager=facts["manager"], runner=facts["runner"], package_globs=facts["package_globs"],
                  source_globs=facts["source_globs"], test_style=facts["test_style"], test_suffix=facts["test_suffix"],
                  source="connected", notes=tuple(facts["evidence"]))
    m = facts["manager"]
    if facts["language"] == "python":
        if facts.get("workspace"):
            install = "uv sync --all-packages --all-groups || uv sync --all-packages"
        else:
            install = "cd {package_dir} && (" + PY_INSTALL[m] + ")" + (" && pip install -q pytest" if m == "pip" else "")
        env = "export CI=1 NO_COLOR=1 POETRY_VIRTUALENVS_IN_PROJECT=true && "
        return RepoProfile(repo, "python", f"python:{facts['python']}-slim", install_cmd=install,
                           test_cmd="cd {package_dir} && " + PY_TEST[m], env=env, **common)
    if facts["runner"] == "node":
        install = "; ".join(f"(cd {d} && {'npm ci' if lock else 'npm install'}) || echo {d} >> /etc/da-install-failures"
                            for d, lock in facts["installs"])
        test = 'cd {package_dir} || exit 1; rc=0; for f in {test_path}; do echo "## $f"; node "$f" || rc=1; done; exit $rc'
        return RepoProfile(repo, "javascript", f"node:{facts['node']}-slim", install_cmd=(install + "; true") if install else "true",
                           test_cmd=test, env=JS_ENV["npm"], test_glob=" ".join(facts["test_files"]), **common)
    berry = bool(re.match(r"yarn@[2-9]", facts["package_manager_field"]))
    install = {"pnpm": "pnpm install --frozen-lockfile --store-dir /work/.pnpm-store",
               "yarn": "yarn install --immutable" if berry else "yarn install --frozen-lockfile",
               "npm": "npm ci" if facts["lock"] else "npm install"}[m]
    monorepo = facts["packages"] != ["."]
    if facts["root_build"]:
        build = f"{m} run build"
    elif monorepo:  # each package's own build, where it has one (tests often import a sibling's built output)
        build = {"pnpm": "pnpm -r --if-present build", "npm": "npm run build --workspaces --if-present",
                 "yarn": "yarn workspaces foreach -A run build" if berry else ""}[m]
    else:
        build = ""
    exe = {"pnpm": "pnpm exec", "yarn": "yarn", "npm": "npx --no-install"}[m]
    test = f"cd {{package_dir}} && {exe} {facts['runner']}" + (" run" if facts["runner"] == "vitest" else "") + " {test_path}"
    return RepoProfile(repo, facts["language"], f"node:{facts['node']}-slim", install_cmd=install, test_cmd=test,
                       build_cmd=build, env=JS_ENV[m], **common)


def dockerfile(p: RepoProfile, packages: list[str]) -> str:
    """The template recipe: the repo at its commit, installed (and built) once, with the network on."""
    env = p.env.rstrip().removesuffix("&&").strip()
    out = (f"# Written by connect.py for {p.repo} at {p.base_commit[:7]}\nFROM {p.image}\n"
           "RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates "
           "&& rm -rf /var/lib/apt/lists/*\n")
    if p.language == "python":
        out += "RUN pip install --no-cache-dir uv" + (" poetry" if p.manager == "poetry" else "") + "\n"
    out += (f"WORKDIR /work\nRUN git init -q && git remote add origin https://github.com/{p.repo}.git \\\n"
            f" && git fetch -q --depth 1 origin {p.base_commit} && git checkout -q FETCH_HEAD \\\n"
            " && printf '.corepack/\\n.pnpm-store/\\n.bin/\\n.da-logs/\\n.venv/\\n' > .git/info/exclude\n")
    if p.language == "python" and "{package_dir}" in p.install_cmd:
        # one package failing to install doesn't sink the rest; the prove step shows which ones work
        out += (f"RUN {env} && for d in {' '.join(packages[:MAX_PACKAGES])}; do (" + p.install_cmd.format(package_dir="$d")
                + ') || echo "$d" >> /etc/da-install-failures; done; touch /etc/da-install-failures\n')
    else:
        out += f"RUN {env} && {p.install_cmd}\n"
    if p.build_cmd:
        out += f"RUN {env} && {p.build_cmd}\n"
    # what the image itself declares: the secret probe flags only variables beyond these
    return out + "RUN env | cut -d= -f1 | sort > /etc/da-env-baseline\n"


# ── the pipeline, with its progress in MongoDB ────────────────────────────────────────────────────
class Progress:
    """One document per repo in `connects`, rewritten whole at each change (the Connect page reads it)."""

    def __init__(self, repo: str, coll=None):
        if coll is None:
            from .store import db
            coll = db()["connects"]
        self.coll = coll
        self.doc = {"_id": repo, "status": "running", "started_at": now(), "finished_at": "", "why": "", "log": [],
                    "steps": [{"key": k, "label": label, "status": "waiting", "detail": ""} for k, label in STEPS]}
        self._save()

    def _save(self):
        self.coll.replace_one({"_id": self.doc["_id"]}, self.doc, upsert=True)

    def step(self, key: str, status: str, detail: str = ""):
        for s in self.doc["steps"]:
            if s["key"] == key:
                s.update(status=status, detail=detail[:800], at=now())
        self._save()

    def log(self, line: str):
        self.doc["log"] = (self.doc["log"] + [str(line)[:300]])[-40:]
        self._save()

    def finish(self, status: str, why: str = ""):
        self.doc.update(status=status, why=why[:800], finished_at=now())
        self._save()


def connect(repo: str, coll=None, repos=None, build=None, prove=None, workdir: Path | None = None) -> dict:
    """The six steps for owner/name. build / prove / the collections are swappable for tests."""
    from .checkout import ensure_base, mark_template
    from .config import CFG
    pr, step = Progress(repo, coll), "read"
    try:
        pr.step("read", "running")
        meta = github_read.api(f"repos/{repo}")
        if not meta:
            raise ValueError("GitHub has no such repository (or it is private)")
        if meta.get("private"):
            raise ValueError("it is private: the test sandbox fetches code without a password, so only public repos work")
        branch = meta.get("default_branch") or "main"
        commit = (github_read.api(f"repos/{repo}/commits/{branch}") or {}).get("sha")
        if not commit:
            raise ValueError(f"could not read the latest commit on {branch}")
        root = ensure_base(RepoProfile(repo, "", "", "", "", base_commit=commit))
        pr.step("read", "done", f"{branch} at {commit[:7]}; GitHub says the main language is {meta.get('language') or 'unknown'}")

        step = "detect"
        pr.step("detect", "running")
        facts = detect(root, meta.get("language") or "")
        pr.step("detect", "done", "; ".join(facts["evidence"]))

        step = "draft"
        p = draft(repo, facts, commit, branch)
        recipe = dockerfile(p, facts["packages"])
        out = (workdir or CFG.runs_dir / "_connect") / p.e2b_template
        out.mkdir(parents=True, exist_ok=True)
        (out / "e2b.Dockerfile").write_text(recipe)
        for line in (f"install: {p.install_cmd}", f"build: {p.build_cmd or 'none'}", f"test: {p.test_cmd}"):
            pr.log(line)
        pr.step("draft", "done", f"Installs with {p.manager}" + (", then builds" if p.build_cmd else "") +
                f"; runs tests with {p.runner}; image {p.image}")

        step = "build"
        pr.step("build", "running", "usually 5 to 15 minutes")
        (build or build_template)(p, out / "e2b.Dockerfile", pr.log)
        mark_template(root, p)
        pr.step("build", "done", f"template {p.e2b_template}")

        step = "prove"
        pr.step("prove", "running")
        baseline = (prove or prove_tests)(p, root, facts["packages"], pr.log)
        passed = [d for d, r in baseline if r == "pass"]
        detail = f"{len(passed)} of {len(baseline)} test suites pass with the network off: " + \
            ", ".join(f"{d} {r}" for d, r in baseline)
        if not passed:
            pr.step("prove", "failed", detail)
            raise ValueError("no package's tests pass in the test sandbox with the network off, so a fix could not be judged")
        pr.step("prove", "done", detail)

        step = "save"
        p = replace(p, baseline=tuple(baseline), connected_at=now())
        if p.runner == "node":  # a fix must keep green what was green: the files that passed here
            p = replace(p, test_glob=" ".join(passed))
        if repos is None:
            from .store import db
            repos = db()["repos"]
        repos.replace_one({"_id": repo}, {"_id": repo, "status": "connected", "profile": to_doc(p),
                                          "connected_at": p.connected_at, "recipe": recipe}, upsert=True)
        pr.step("save", "done", "issues from this repo can now be run")
        pr.finish("connected")
        return {"status": "connected", "profile": to_doc(p)}
    except Exception as ex:
        if next(s for s in pr.doc["steps"] if s["key"] == step)["status"] != "failed":
            pr.step(step, "failed", f"{ex}")
        pr.finish("failed", f"{dict(STEPS)[step]}: {ex}")
        return {"status": "failed", "step": step, "why": str(ex)}


def build_template(p: RepoProfile, recipe: Path, log) -> None:
    from e2b import Template
    Template.build(Template().from_dockerfile(str(recipe)), alias=p.e2b_template, cpu_count=2, memory_mb=4096,
                   on_build_logs=lambda entry: log(str(getattr(entry, "message", entry))))


def prove_tests(p: RepoProfile, root: Path, packages: list[str], log) -> list[tuple[str, str]]:
    """The packages with the most test files (up to PROVE_PACKAGES), each suite once, network off, in the template.
    Plain Node: each test file on its own (up to PROVE_FILES), since one file needing the network would sink the rest."""
    from . import lang as langs
    from .sandbox_e2b import close, run
    lang = langs.of(p)
    if p.runner == "node":
        runs = [(f, lang.test_command(".", f)) for f in p.test_glob.split()[:PROVE_FILES]]
    else:
        pats = ["test_*.py", "*_test.py"] if p.language == "python" else ["*.test.*", "*.spec.*"]
        counts = {d: len(_test_files(root, d, pats, 200)) for d in packages}
        runs = [(d, lang.test_command(d)) for d in sorted((d for d in packages if counts[d]), key=lambda d: -counts[d])[:PROVE_PACKAGES]]
    out = []
    for d, cmd in runs:
        t0 = time.monotonic()
        r = run(cmd, root, timeout=PROVE_TIMEOUT_S)
        verdict = "pass" if r.returncode == 0 else "fail"
        tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-1:] or [""]
        out.append((d, verdict))
        log(f"{d}: {verdict} in {time.monotonic() - t0:.0f} s · {tail[0][:160]}")
    close(root)
    return out


def recent(limit: int = 12) -> list[dict]:
    """The latest connections tried, newest first (the Connect page lists the ones not connected yet)."""
    from .config import CFG
    from .store import client, reachable
    try:
        if not reachable():
            return []
        return list(client(1500)[CFG.db_name]["connects"].find().sort("started_at", -1).limit(limit))
    except Exception:
        return []


def status(repo: str) -> dict | None:
    from .config import CFG
    from .store import client, reachable
    try:
        if not reachable():
            return None
        return client(1500)[CFG.db_name]["connects"].find_one({"_id": repo})
    except Exception:
        return None


def run_cli(text: str) -> int:
    try:
        repo = parse_repo(text)
    except ValueError as ex:
        print(ex)
        return 2
    print(f"connecting {repo} (progress: the Connect a repo page, or MongoDB connects/{repo})", flush=True)
    try:
        res = connect(repo)
    except Exception as ex:  # nothing could be saved (the database is down): one plain line, the page shows it
        print(f"Nothing was saved: {type(ex).__name__}: {str(ex)[:200]}", flush=True)
        return 1
    print(json.dumps({k: v for k, v in res.items() if k != "profile"}))
    return 0 if res["status"] == "connected" else 1
