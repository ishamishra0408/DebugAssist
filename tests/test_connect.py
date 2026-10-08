"""Connect a repo: the setup comes from the repo's own files (no AI), each choice carries its evidence, the template
recipe installs once with the network on, and a repo is saved only when its tests pass with the network off.
Run against small fake repos, a fake GitHub, a fake E2B and an in-memory collection (no account, no network)."""
import copy
import json
import subprocess

import pytest

from debug_assist import connect as C
from debug_assist.profiles import from_doc


class Coll:
    """Enough of a MongoDB collection for connect.py: whole-document writes."""

    def __init__(self):
        self.docs, self.history = {}, []

    def replace_one(self, flt, doc, upsert=False):
        self.docs[flt["_id"]] = copy.deepcopy(doc)
        self.history.append(copy.deepcopy(doc))

    def find_one(self, flt, *a):
        return self.docs.get(flt["_id"])


def write(root, files: dict):
    for rel, text in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text if isinstance(text, str) else json.dumps(text))
    return root


@pytest.fixture
def pnpm_repo(tmp_path):
    return write(tmp_path / "pnpm", {
        "package.json": {"name": "root", "scripts": {"build": "turbo build"}, "engines": {"node": ">=18"},
                         "packageManager": "pnpm@9.1.0", "devDependencies": {"vitest": "2"}},
        "pnpm-lock.yaml": "", "pnpm-workspace.yaml": "packages:\n  - packages/*\n  - examples/*\n", "tsconfig.json": "{}",
        "packages/core/package.json": {"name": "@acme/core"}, "packages/core/src/a.ts": "",
        "packages/core/src/a.test.ts": "", "packages/core/src/b.test.ts": "",
        "packages/web/package.json": {"name": "@acme/web"}, "packages/web/src/w.test.ts": "",
        "examples/demo/package.json": {"name": "demo"},
    })


@pytest.fixture
def uv_libs(tmp_path):
    return write(tmp_path / "uvlibs", {
        "libs/core/pyproject.toml": '[project]\nname="core"\nrequires-python=">=3.10,<4.0"\n[dependency-groups]\ntest=["pytest"]\n',
        "libs/core/uv.lock": "", "libs/core/core/__init__.py": "",
        "libs/core/tests/unit_tests/test_a.py": "", "libs/core/tests/unit_tests/test_b.py": "",
        "libs/partners/openai/pyproject.toml": '[project]\nname="openai-x"\n', "libs/partners/openai/uv.lock": "",
        "libs/partners/openai/tests/unit_tests/test_c.py": "",
        "docs/pyproject.toml": '[project]\nname="docs"\n',
    })


def test_a_pnpm_monorepo_is_read_from_its_own_files(pnpm_repo):
    f = C.detect(pnpm_repo)
    assert (f["language"], f["manager"], f["runner"]) == ("typescript", "pnpm", "vitest")
    assert f["packages"] == ["packages/core", "packages/web"]                   # examples/ is not a package to fix
    assert f["package_globs"] == ("packages/*",) and f["source_globs"] == ("packages/*/src/**",)
    assert (f["test_style"], f["test_suffix"], f["node"]) == ("beside", ".test.ts", 22)
    assert any("pnpm-lock.yaml" in e for e in f["evidence"]) and any("pnpm-workspace.yaml" in e for e in f["evidence"])
    p = C.draft("acme/widgets", f, "a" * 40, "main")
    assert p.install_cmd.startswith("pnpm install --frozen-lockfile") and p.build_cmd == "pnpm run build"
    assert p.test_cmd == "cd {package_dir} && pnpm exec vitest run {test_path}" and p.image == "node:22-slim"
    assert p.e2b_template == "debugassist-acme-widgets-aaaaaaa" and p.source == "connected"
    recipe = C.dockerfile(p, f["packages"])
    assert f"git fetch -q --depth 1 origin {'a' * 40}" in recipe and "RUN export COREPACK_HOME" in recipe
    assert recipe.rstrip().endswith("/etc/da-env-baseline") and "pnpm run build" in recipe


def test_an_npm_package_with_jest_and_a_tests_folder(tmp_path):
    root = write(tmp_path / "npm", {"package.json": {"name": "x", "devDependencies": {"jest": "29"}},
                                    "src/a.js": "", "__tests__/a.test.js": "", "__tests__/b.test.js": "", ".nvmrc": "v20.11\n"})
    f = C.detect(root)
    assert (f["language"], f["manager"], f["runner"], f["packages"]) == ("javascript", "npm", "jest", ["."])
    assert (f["test_style"], f["test_suffix"], f["node"], f["source_globs"]) == ("tests", ".test.js", 20, ("src/**",))
    p = C.draft("acme/x", f, "b" * 40, "master")
    assert p.install_cmd == "npm install" and p.build_cmd == "" and p.default_branch == "master"   # no lock file
    assert p.test_cmd == "cd {package_dir} && npx --no-install jest {test_path}"


def test_python_packages_install_one_by_one_and_a_failure_does_not_sink_the_rest(uv_libs):
    f = C.detect(uv_libs)
    assert f["language"] == "python" and f["packages"] == ["libs/core", "libs/partners/openai"]   # docs/ skipped
    assert (f["manager"], f["test_style"], f["python"], f["workspace"]) == ("uv", "tests/unit_tests", "3.12", False)
    p = C.draft("acme/py", f, "c" * 40, "main")
    assert p.image == "python:3.12-slim" and p.test_cmd == "cd {package_dir} && uv run --no-sync pytest -q {test_path}"
    recipe = C.dockerfile(p, f["packages"])
    assert "for d in libs/core libs/partners/openai; do (cd $d && (uv sync --group test" in recipe
    assert '|| echo "$d" >> /etc/da-install-failures' in recipe and "pip install --no-cache-dir uv" in recipe


def test_a_uv_workspace_installs_once_at_the_top(tmp_path):
    root = write(tmp_path / "ws", {
        "pyproject.toml": '[tool.uv.workspace]\nmembers = ["packages/*"]\n', "uv.lock": "",
        "packages/a/pyproject.toml": '[project]\nname="a"\nrequires-python=">=3.9,<3.12"\n', "packages/a/tests/test_a.py": "",
    })
    f = C.detect(root)
    assert f["workspace"] and f["packages"] == ["packages/a"] and f["python"] == "3.11"
    p = C.draft("acme/ws", f, "d" * 40, "main")
    assert p.install_cmd.startswith("uv sync --all-packages") and "for d in" not in C.dockerfile(p, f["packages"])


def test_a_poetry_project(tmp_path):
    root = write(tmp_path / "po", {"pyproject.toml": '[tool.poetry]\nname="p"\n', "poetry.lock": "", "tests/test_x.py": ""})
    p = C.draft("acme/po", C.detect(root), "e" * 40, "main")
    assert p.manager == "poetry" and "poetry run pytest" in p.test_cmd and "poetry" in C.dockerfile(p, ["."])


def test_both_languages_github_decides(tmp_path):
    root = write(tmp_path / "both", {"package.json": {"name": "ui"}, "pyproject.toml": '[project]\nname="svc"\n',
                                     "tests/test_s.py": ""})
    assert C.detect(root, "Python")["language"] == "python" and C.detect(root, "TypeScript")["language"] == "javascript"


def test_not_a_supported_repo(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    with pytest.raises(ValueError, match="not a JavaScript, TypeScript or Python repo"):
        C.detect(tmp_path)


def test_repo_links():
    assert C.parse_repo("https://github.com/acme/widgets") == "acme/widgets"
    assert C.parse_repo("https://github.com/acme/widgets.git") == "acme/widgets" == C.parse_repo("acme/widgets/")
    with pytest.raises(ValueError):
        C.parse_repo("https://gitlab.com/acme/widgets")


# ── the pipeline end to end, with fakes ─────────────────────────────────────────────────────────
@pytest.fixture
def fake_world(monkeypatch, uv_libs, tmp_path):
    for c in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b"]):
        subprocess.run(c, cwd=uv_libs, check=True)
    gh = {"repos/acme/py": {"default_branch": "main", "language": "Python", "private": False},
          "repos/acme/py/commits/main": {"sha": "f" * 40}}
    monkeypatch.setattr(C.github_read, "api", lambda path, *a: gh.get(path))
    monkeypatch.setattr("debug_assist.checkout.ensure_base", lambda prof: uv_libs)
    return {"root": uv_libs, "gh": gh, "connects": Coll(), "repos": Coll(), "work": tmp_path / "work", "built": []}


def _connect(w, prove):
    return C.connect("acme/py", coll=w["connects"], repos=w["repos"], workdir=w["work"],
                     build=lambda p, recipe, log: (w["built"].append(recipe.read_text()), log("built")), prove=prove)


def test_a_repo_whose_tests_pass_offline_is_saved_connected(fake_world):
    w = fake_world
    res = _connect(w, lambda p, root, pkgs, log: [("libs/core", "pass"), ("libs/partners/openai", "fail")])
    assert res["status"] == "connected"
    saved = w["repos"].docs["acme/py"]
    p = from_doc(saved["profile"])
    assert saved["status"] == "connected" and p.baseline == (("libs/core", "pass"), ("libs/partners/openai", "fail"))
    assert p.e2b_template == "debugassist-acme-py-fffffff" and saved["recipe"] == w["built"][0]
    assert (w["root"] / ".git" / "da-template").read_text() == p.e2b_template      # the code copy knows its template
    prog = w["connects"].docs["acme/py"]
    assert prog["status"] == "connected" and [s["status"] for s in prog["steps"]] == ["done"] * 6
    assert "built" in prog["log"] and "1 of 2" in prog["steps"][4]["detail"]
    assert any(s["steps"][3]["status"] == "running" for s in w["connects"].history)   # the page saw it working


def test_no_passing_tests_means_not_connected(fake_world):
    w = fake_world
    res = _connect(w, lambda *a: [("libs/core", "fail")])
    assert res["status"] == "failed" and res["step"] == "prove" and "acme/py" not in w["repos"].docs
    prog = w["connects"].docs["acme/py"]
    assert prog["status"] == "failed" and prog["steps"][4]["status"] == "failed" and "Run its tests" in prog["why"]


def test_a_private_repo_is_refused_before_anything_is_built(fake_world):
    w = fake_world
    w["gh"]["repos/acme/py"]["private"] = True
    res = _connect(w, lambda *a: [])
    assert res == {"status": "failed", "step": "read", "why": res["why"]} and "private" in res["why"] and not w["built"]


def test_a_build_failure_names_the_step(fake_world):
    w = fake_world

    def boom(*a):
        raise RuntimeError("E2B build failed: exit 1 at RUN uv sync")
    res = C.connect("acme/py", coll=w["connects"], repos=w["repos"], workdir=w["work"], build=boom, prove=None)
    assert res["step"] == "build" and "Build its test sandbox: E2B build failed" in w["connects"].docs["acme/py"]["why"]


def test_prove_runs_the_biggest_suites_offline_and_ends_the_sandbox(fake_world, monkeypatch):
    calls, closed = [], []
    monkeypatch.setattr("debug_assist.sandbox_e2b.run", lambda cmd, wd, network=False, timeout=0: (
        calls.append((cmd, network)), subprocess.CompletedProcess(cmd, 0 if "core" in cmd else 1, "3 passed", ""))[1])
    monkeypatch.setattr("debug_assist.sandbox_e2b.close", lambda wd: closed.append(wd))
    p = C.draft("acme/py", C.detect(fake_world["root"]), "f" * 40, "main")
    out = C.prove_tests(p, fake_world["root"], ["libs/partners/openai", "libs/core"], lambda line: None)
    assert out == [("libs/core", "pass"), ("libs/partners/openai", "fail")]           # most test files first
    assert calls[0][0].endswith("cd libs/core && uv run --no-sync pytest -q ") and not any(n for _, n in calls)
    assert closed == [fake_world["root"]]


def test_a_repo_without_one_top_level_setup_says_what_it_found(tmp_path):
    write(tmp_path, {"plugins/guard/package.json": {"name": "g"}, "render/package.json": {"name": "r"},
                     "tests/guard.test.mjs": "", ".nvmrc": "20\n"})
    with pytest.raises(ValueError, match=r"no package.json at the top of the repo.*found one in: plugins/guard, render"):
        C.detect(tmp_path)
