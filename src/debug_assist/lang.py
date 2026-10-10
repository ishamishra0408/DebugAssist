"""Language adapters: everything the bug pipeline does differently for a JavaScript/TypeScript repo and a Python one,
in one place. testwriter, fixer, guard and context ask the adapter; they never assume a layout.

  where     source and test files live (git pathspecs from the profile's source_globs), which package a file is in
  tests     how a new test is named and placed (beside the example test), the example test, its setup and a pattern case
  running   the command for one test file or one package's suite, and the verbose flag that lists every case
  reading   the per-case results and failure blocks of vitest / jest / pytest / node:test output

Three kinds: JS (vitest, jest), NodeScripts (test files run with plain `node <file>`: node:test or a script that
exits 1 on failure; NoLeakMCP, 2026-10-08) and Python (pytest).
  writing   the test framework and rules the AI is given, and the checks a draft must pass (decided in code)

vercel/ai's built-in profile keeps its exact behaviour: its defaults (packages/*/src/**, .test.ts, vitest) are the
JavaScript adapter's defaults.
"""
import html
import json
import re
from fnmatch import fnmatch
from pathlib import Path

SKIP = {"node_modules", ".git", "dist", "build", ".venv", "__pycache__", "examples", "docs"}


def is_test_path(path: str) -> bool:
    """A test, fixture or snapshot in either language: never a place for a fix, never the cause."""
    return bool(re.search(r"\.(test|spec)(-d)?\.[cm]?[tj]sx?$|__fixtures__/|__snapshots__/|__tests__/|/da-repro-|"
                          r"(^|/)tests?/|(^|/)test_[^/]+\.py$|_test\.py$|(^|/)conftest\.py$", path))


class _View:
    """The profile, with RepoProfile's defaults for anything it leaves out (tests pass partial profiles)."""

    def __init__(self, profile):
        self._p = profile

    def __getattr__(self, k):
        from dataclasses import MISSING, fields
        from .profiles import RepoProfile
        if self._p is not None and hasattr(self._p, k):
            return getattr(self._p, k)
        f = next((f for f in fields(RepoProfile) if f.name == k), None)
        if f is None:
            raise AttributeError(k)
        return f.default if f.default is not MISSING else ""


class Lang:
    key = ""
    framework = ""
    fence = ""
    marker = ""          # the file that makes a folder a package
    EXCLUDE: list = []   # git pathspecs that drop tests and non-code from source searches

    def __init__(self, profile=None):
        self.p = _View(profile)

    # ── where ────────────────────────────────────────────────────────────────────────────────────
    def source_specs(self) -> list[str]:
        return ["." if g in ("**", ".") else g for g in self.p.source_globs]

    def pathspec(self) -> list[str]:
        """For `git grep … -- <these>` / `git ls-files -- <these>`: source files, tests excluded."""
        return [*self.source_specs(), *self.EXCLUDE]

    def is_source(self, path: str) -> bool:
        return not is_test_path(path) and any(g in ("**", ".") or fnmatch(path, g) for g in self.p.source_globs)

    def package_dirs(self, checkout: Path, need_marker: bool = True) -> list[str]:
        out = []
        for g in self.p.package_globs:
            for d in ([Path(checkout)] if g == "." else sorted(Path(checkout).glob(g))):
                rel = d.relative_to(checkout).as_posix()
                ok = (d / self.marker).exists() if need_marker else d.is_dir()
                if ok and not (set(Path(rel).parts) & SKIP) and rel not in out:
                    out.append(rel)
        return out

    def package_of(self, path: str) -> str:
        """The package folder a repo path belongs to ("packages/ai", "libs/partners/openai", or "." for one package)."""
        parts = path.split("/")
        for g in sorted(self.p.package_globs, key=lambda g: -len(g.split("/"))):
            if g == ".":
                continue
            k = len(g.split("/"))
            if len(parts) > k and fnmatch("/".join(parts[:k]), g):
                return "/".join(parts[:k])
        return "."

    @staticmethod
    def within(path: str, pkg_dir: str) -> str:
        return path if pkg_dir in (".", "") else str(Path(path).relative_to(pkg_dir))

    # ── running ──────────────────────────────────────────────────────────────────────────────────
    def test_command(self, pkg_dir: str, test_path: str = "", extra: str = "", where=None) -> str:
        """One test file (test_path repo-relative) or, with no test_path, the package's whole suite. where: the copy of
        the code it runs in (its package.json says which script), else the repo's prepared copy."""
        rel = self.within(test_path, pkg_dir) if test_path else ""
        return (self.p.env + self.p.test_cmd.format(package=Path(pkg_dir).name, package_dir=pkg_dir,
                                                    test_path=f"{rel} {extra}".strip(),
                                                    script=self.test_script(pkg_dir, where))).rstrip()

    def test_script(self, pkg_dir: str, where=None) -> str:
        """The package's own script for its node tests: `test:node` where it has one, else `test` (vercel/ai's vue,
        react, svelte and otel have only `test`; found 2026-10-10). Read from the run's own copy, on main (review of run
        #22543: it was read from the saved copy, which can be days older), else the repo's prepared copy."""
        try:
            from .checkout import base_path
            root = Path(where) if where else base_path(self.p)
            scripts = json.loads((root / pkg_dir / "package.json").read_text()).get("scripts") or {}
        except (OSError, ValueError, AttributeError, TypeError):
            return "test:node"
        return "test:node" if "test:node" in scripts or "test" not in scripts else "test"

    def rebuild_command(self, package_names: list[str]) -> str:
        """Rebuild changed packages after an edit (monorepos whose tests import a sibling's built output)."""
        if not self.p.build_cmd or not package_names:
            return ""
        names = sorted(package_names)
        if self.p.manager == "pnpm":
            flt = " ".join(f"--filter '{n}'" for n in names)
            return self.p.env + (f"pnpm {flt} build" if self.p.source == "built-in" else f"pnpm {flt} run --if-present build")
        if self.p.manager == "npm":
            return self.p.env + "npm run build --if-present " + " ".join(f"-w '{n}'" for n in names)
        if self.p.manager == "yarn":
            return self.p.env + " && ".join(f"yarn workspace '{n}' run build" for n in names)
        return ""

    # ── tests ────────────────────────────────────────────────────────────────────────────────────
    def test_files(self, checkout: Path, pkg_dir: str) -> list[Path]:
        raise NotImplementedError

    def example_test(self, checkout: Path, source: str) -> str:
        """The existing test file the new one is modelled on and placed beside: the one that best matches the source
        (same name, same folders, mentions it), else the largest in the package."""
        checkout, pkg = Path(checkout), self.package_of(source)
        stem, src_parts = Path(source).stem, set(Path(source).parent.parts)
        best = None
        for f in self.test_files(checkout, pkg):
            rel = f.relative_to(checkout).as_posix()
            if "/da-" in rel or Path(rel).name.startswith("test_da_"):
                continue
            text = f.read_text(errors="ignore")[:200_000]
            score = (5 * (self.tested_stem(f.name) == stem) + len(set(Path(rel).parent.parts) & src_parts)
                     + 3 * bool(re.search(rf"\b{re.escape(stem)}\b", text)) + self.place_bonus(rel)
                     + 6 * (Path(source).as_posix() in text))  # it imports the very file ("../plugins/x/index.js")
            key = (score, len(text))
            if best is None or key > best[0]:
                best = (key, rel)
        if not best:
            from .testwriter import WriterRefused
            raise WriterRefused(f"no test file in {pkg} to follow")
        return best[1]

    def place_bonus(self, rel: str) -> int:
        return 0

    def tested_stem(self, name: str) -> str:
        raise NotImplementedError

    def new_test(self, example: str, kind: str, issue: int, label: str = "", n: int | None = None) -> str:
        """Repo path of a new test file beside the example (kind: repro | guard)."""
        raise NotImplementedError

    def header(self, text: str) -> str:
        raise NotImplementedError

    def pattern_case(self, text: str, focus: str = "") -> str:
        raise NotImplementedError

    # ── reading output ───────────────────────────────────────────────────────────────────────────
    VERBOSE = ""

    def cases(self, output: str) -> dict:
        raise NotImplementedError

    def failure_blocks(self, output: str) -> dict:
        raise NotImplementedError

    # ── writing ──────────────────────────────────────────────────────────────────────────────────
    def definition_patterns(self, name: str) -> list[str]:
        raise NotImplementedError

    def imports(self, src: str) -> list[tuple[str, str, bool]]:
        """(name, module, type_only) for names imported from another package (not relative imports)."""
        raise NotImplementedError

    def validate(self, content: str, rung_name: str) -> None:
        raise NotImplementedError


# ── JavaScript / TypeScript: vitest or jest ──────────────────────────────────────────────────────
class JS(Lang):
    key, marker = "js", "package.json"
    EXCLUDE = [":!*.test.ts", ":!*.test.tsx", ":!*.test-d.ts", ":!*.spec.ts", ":!*.spec.tsx", ":!*.test.js",
               ":!*.spec.js", ":!*.test.jsx", ":!*.test.mjs", ":!*.test.cjs", ":!*.spec.mjs", ":!*__fixtures__*",
               ":!*__snapshots__*", ":!*__tests__*", ":!*.md"]

    def __init__(self, profile=None):
        super().__init__(profile)
        self.framework = self.p.runner if self.p.runner in ("vitest", "jest") else "vitest"
        self.fence = "ts" if self.p.test_suffix.endswith(("ts", "tsx")) else "js"
        self.VERBOSE = "--reporter=verbose" if self.framework == "vitest" else "--verbose"

    def test_files(self, checkout, pkg_dir):
        base = Path(checkout) / pkg_dir
        out = []
        for pat in ("*.test.ts", "*.test.tsx", "*.spec.ts", "*.test.js", "*.spec.js", "*.test.jsx"):
            out += [f for f in base.rglob(pat) if not (set(f.relative_to(checkout).parts) & SKIP)]
        return out

    def tested_stem(self, name):
        return re.sub(r"\.(test|spec)\.[cm]?[tj]sx?$", "", name)

    def example_test(self, checkout, source):
        """vercel/ai's rule first (the test named after the source, else the largest test beside it), then the
        general search over the package."""
        checkout = Path(checkout)
        beside = Path(source).with_name(Path(source).stem + self.p.test_suffix)
        if (checkout / beside).exists():
            return str(beside)
        tests = sorted((checkout / source).parent.glob(f"*{self.p.test_suffix}"), key=lambda p: -p.stat().st_size)
        tests = [t for t in tests if not t.name.startswith("da-")]
        if tests:
            return str(tests[0].relative_to(checkout))
        return super().example_test(checkout, source)

    def new_test(self, example, kind, issue, label="", n=None):
        suffix = next((s for s in (".test.tsx", ".test.ts", ".spec.ts", ".test.js", ".spec.js", ".test.jsx")
                       if example.endswith(s)), self.p.test_suffix)
        # keep the example's environment part: vercel/ai's vue and react run only *.ui.test.ts(x) (found 2026-10-10)
        env = re.search(r"(\.(?:ui|node|edge|browser|dom))\.(?:test|spec)\.[cm]?[tj]sx?$", example)
        suffix = (env.group(1) + suffix) if env else suffix
        name = f"da-{kind}-{issue}" + (f"-{label}" if label else "") + (f"-{n}" if n is not None else "") + suffix
        return str(Path(example).with_name(name))

    def header(self, text):
        first = re.search(r"^describe\(", text, re.M)
        return text[:first.start()] if first else text[:4000]

    def pattern_case(self, text, focus=""):
        """One existing test, whole: a streaming test when there is one (the by-hand run of #21439 copied that
        shape), else one that uses the focus's words, else the first."""
        lines, found = text.splitlines(), []
        words = [w for w in re.findall(r"[A-Za-z]{5,}", focus)]
        for i, l in enumerate(lines):
            if re.match(r"\s+(it|test)\(", l):
                indent = len(l) - len(l.lstrip())
                for j in range(i + 1, min(len(lines), i + 120)):
                    if lines[j].startswith(" " * indent + "});"):
                        found.append("\n".join(lines[i:j + 1]))
                        break
        for case in found:
            if "stream-chunks" in case:
                return case
        for case in found:
            if any(w in case for w in words):
                return case
        return found[0] if found else ""

    def cases(self, output):
        clean = html.unescape(re.sub(r"\x1b\[[0-9;]*m", "", output))  # vitest's verbose reporter prints &gt; for '>'
        passed = re.findall(r"^\s*[✓√]\s+(.+?)(?:\s+\(?\d+\s?ms\)?)?$", clean, re.M)
        failed = re.findall(r"^\s*[×✗✕]\s+(.+?)(?:\s+\(?\d+\s?ms\)?)?$", clean, re.M)
        return {"passed": passed, "failed": failed}

    def failure_blocks(self, output):
        """vitest's ' FAIL  file > describe > case' sections (jest: '● describe › case'): case header → output."""
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        parts = re.split(r"(?m)^ FAIL  " if " FAIL  " in clean else r"(?m)^\s*● ", clean)[1:]
        heads = [p.splitlines()[0] for p in parts]
        bodies = ["\n".join(p.splitlines()[1:25]) for p in parts]
        # vitest prints cases that failed with the SAME error as consecutive headers sharing one block (trial
        # 2026-10-07: the first case of a pair looked like it showed nothing and was called broken)
        for i in range(len(bodies) - 2, -1, -1):
            if not bodies[i].strip():
                bodies[i] = bodies[i + 1]
        return dict(zip(heads, bodies))

    def definition_patterns(self, name):
        return [rf"(class|function|const|let|interface|type|enum)[[:space:]]+{name}([^[:alnum:]_$]|$)",
                # and class methods: `  async doStream(` / `  private finishToolCall(` (Opus asked for doStream)
                rf"^[[:space:]]+(public |private |protected |static |async |get )*{name}[[:space:]]*[<(]"]

    _IMPORT = re.compile(r"import\s+(?:type\s+)?\{([^}]+)\}\s+from\s+['\"]([^'\"]+)['\"]", re.S)

    def imports(self, src):
        out = []
        for m in self._IMPORT.finditer(src):
            if m.group(2).startswith("."):
                continue
            for part in m.group(1).split(","):
                is_type = part.strip().startswith("type ")
                n = part.strip().removeprefix("type ").split(" as ")[-1].strip()
                if re.fullmatch(r"[A-Za-z_$][\w$]*", n):
                    out.append((n, m.group(2), is_type))
        return out

    def validate(self, content, rung_name):
        from .testwriter import WriterRefused
        if "expect(" not in content:
            raise WriterRefused("the test asserts nothing (no expect)")
        if re.search(r"\b(it|describe|test)\.only\(", content):
            raise WriterRefused(".only would hide other cases")
        if re.search(r"process\.env|https?://(?!my\.api\.com|localhost|127\.0\.0\.1)[\w.-]+\.\w", content):
            raise WriterRefused("the test reaches for env vars or a real host; the sandbox has neither")
        if rung_name == "integration" and not ("__fixtures__/" in content and re.search(r"readFile(Sync)?\(", content)):
            raise WriterRefused("the integration rung must read a recorded fixture from __fixtures__")
        if rung_name == "unit" and "__fixtures__/" in content:
            raise WriterRefused("the unit rung uses made-up input, not a recorded fixture")

    def rules(self) -> str:
        return (f"- Start with the setup you are given (imports, mocks, helpers), adjusted only as needed. The file is\n"
                f"  saved beside the example test, so its relative imports work unchanged.\n"
                f"- No network, no API keys, no environment variables: mock the network the way the pattern test does.\n"
                f"- Fixture paths are relative to the package directory ({self.framework} runs there).\n"
                f"- Make the failing assertion SHOW the evidence: compare the actual values (toStrictEqual / toEqual on\n"
                f"  the list of parts or the result), never a bare count or boolean, so the failure prints what went wrong.\n"
                f"- Do not test anything else. One describe block, one to three it() cases.")

    def guard_rules(self) -> str:
        return "Parametrise it with it.each over every trigger of the class that this code can meet"


# ── Python: pytest ───────────────────────────────────────────────────────────────────────────────
class Python(Lang):
    key, marker, framework, fence = "python", "pyproject.toml", "pytest", "python"
    EXCLUDE = [":!*/tests/*", ":!tests/*", ":!*/test/*", ":!*/test_*.py", ":!test_*.py", ":!*_test.py",
               ":!*conftest.py", ":!*.md", ":!*.ipynb", ":!*.lock", ":!*/docs/*", ":!*/cookbook/*"]
    VERBOSE = "-vv -rA"   # the profile's command says -q; -vv lifts it to one line per case

    def test_files(self, checkout, pkg_dir):
        base = Path(checkout) / pkg_dir
        return [f for pat in ("test_*.py", "*_test.py") for f in base.rglob(pat)
                if not (set(f.relative_to(checkout).parts) & SKIP)]

    def tested_stem(self, name):
        return re.sub(r"^test_|_test$", "", Path(name).stem)

    def place_bonus(self, rel):
        # unit tests run offline; integration tests call real services, which the sandbox never reaches
        return 2 * ("unit" in rel) - 6 * ("integration" in rel)

    def new_test(self, example, kind, issue, label="", n=None):
        name = f"test_da_{kind}_{issue}" + (f"_{label}" if label else "") + (f"_{n}" if n is not None else "") + ".py"
        return str(Path(example).with_name(name))

    _TOP = re.compile(r"^(?:@|def test_|async def test_|class Test)", re.M)

    def header(self, text):
        first = self._TOP.search(text)
        return text[:first.start()] if first else text[:4000]

    def pattern_case(self, text, focus=""):
        """One existing test function (with its decorators), preferring one that uses the focus's words."""
        found = []
        for m in re.finditer(r"^(?:async )?def test_\w+", text, re.M):
            start = m.start()
            above = text.rfind("\n\n", 0, start)  # a decorator block (one or many lines) sits right above the def
            if above != -1 and text[above + 2:start].lstrip().startswith("@"):
                start = above + 2
            after_def = text.find("\n", m.end())
            nxt = re.compile(r"^(?![)\]}])\S", re.M).search(text, after_def + 1) if after_def != -1 else None
            found.append(text[start:nxt.start() if nxt else len(text)].rstrip()[:6000])
        words = set(re.findall(r"[A-Za-z_]{5,}", focus))
        # the case whose name holds a focus word (test_merge_lists for `merge_lists`), then the one using the most
        def name(c):
            m = re.search(r"def (test_\w+)", c)
            return m.group(1) if m else ""
        best = max(found, key=lambda c: (any(w in name(c) for w in words), sum(w in c for w in words)), default="")
        return best

    def cases(self, output):
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        got = {"passed": [], "failed": []}
        for m in re.finditer(r"^(\S+?\.py::\S+) (PASSED|FAILED|ERROR)\b", clean, re.M):
            name = m.group(1).split("::", 1)[1]
            bucket = got["passed"] if m.group(2) == "PASSED" else got["failed"]
            if name not in bucket:
                bucket.append(name)
        for m in re.finditer(r"^(PASSED|FAILED|ERROR) (\S+?\.py::\S+)", clean, re.M):  # -rA's short summary
            name = m.group(2).split("::", 1)[1]
            bucket = got["passed"] if m.group(1) == "PASSED" else got["failed"]
            if name not in bucket:
                bucket.append(name)
        return got

    def failure_blocks(self, output):
        """pytest's '____ test_name[case] ____' sections: case name → what it printed."""
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        out = {}
        heads = list(re.finditer(r"^_{3,} (.+?) _{3,}$", clean, re.M))
        for i, m in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(clean)
            body = clean[m.end():end]
            body = re.split(r"^={5,}", body, flags=re.M)[0]
            out[m.group(1).strip()] = "\n".join(body.strip().splitlines()[-40:])
        return out

    def definition_patterns(self, name):
        return [rf"^[[:space:]]*(async[[:space:]]+)?def[[:space:]]+{name}[[:space:]]*[\[(]",
                rf"^[[:space:]]*class[[:space:]]+{name}([[:space:](:\[]|$)",
                rf"^{name}[[:space:]]*(:[^=]+)?=[^=]"]

    _FROM = re.compile(r"^from\s+([\w.]+)\s+import\s+(\([^)]*\)|[^\n]+)", re.M)

    def imports(self, src):
        out = []
        type_block = re.search(r"^if TYPE_CHECKING:\n((?:[ \t]+.*\n|\n)+)", src, re.M)
        typed = type_block.group(1) if type_block else ""
        for m in self._FROM.finditer(src):
            mod = m.group(1)
            if mod.startswith(".") or mod.split(".")[0] in ("typing", "__future__", "collections", "dataclasses",
                                                            "abc", "enum", "os", "re", "json", "asyncio", "functools"):
                continue
            for part in m.group(2).strip("()").split(","):
                n = part.strip().split(" as ")[-1].strip()
                if re.fullmatch(r"[A-Za-z_]\w*", n):
                    out.append((n, mod, m.group(0) in typed))
        return out

    def validate(self, content, rung_name):
        from .testwriter import WriterRefused
        if not re.search(r"^\s*assert\b|pytest\.raises\(", content, re.M):
            raise WriterRefused("the test asserts nothing (no assert)")
        if not re.search(r"^(?:async )?def test_\w+", content, re.M) and not re.search(r"^\s+(?:async )?def test_", content, re.M):
            raise WriterRefused("the file has no test function (def test_...)")
        if re.search(r"pytest\.mark\.skip|pytest\.skip\(|pytest\.mark\.xfail", content):
            raise WriterRefused("a skipped or expected-to-fail test proves nothing")
        if re.search(r"https?://(?!localhost|127\.0\.0\.1|example\.(?:com|org)|test\b|testserver)[\w.-]+\.\w", content):
            raise WriterRefused("the test reaches for a real host; the sandbox has no network")

    def rules(self) -> str:
        return ("- Start with the setup you are given (imports, fixtures, helpers), adjusted only as needed. The file is\n"
                "  saved beside the example test, so the same conftest.py fixtures are available.\n"
                "- No network, no API keys: mock the network the way the pattern test does (unittest.mock, respx,\n"
                "  responses, fake clients). Setting a FAKE key with monkeypatch.setenv is fine.\n"
                "- Make the failing assertion SHOW the evidence: `assert actual == expected` on the actual values, never\n"
                "  a bare count or boolean, so pytest prints both sides.\n"
                "- Do not test anything else. One to three test functions, no classes needed.")

    def guard_rules(self) -> str:
        return "Parametrise it with @pytest.mark.parametrize over every trigger of the class that this code can meet"


# ── plain Node: each test file is a script, run with `node <file>` ───────────────────────────────
class NodeScripts(JS):
    """No test framework: a test file is a program that exits 1 when something is wrong (node:test does that too).
    The suite is the list of files that passed when the repo was connected (profile.test_glob)."""
    key = "node"

    def __init__(self, profile=None):
        super().__init__(profile)
        self.framework, self.fence, self.VERBOSE = "node:test", "js", ""

    PATTERNS = ("*.test.mjs", "*.test.js", "*.test.cjs", "*.spec.mjs", "*.spec.js")

    def test_files(self, checkout, pkg_dir):
        base = Path(checkout) / pkg_dir
        return [f for pat in self.PATTERNS for f in base.rglob(pat) if not (set(f.relative_to(checkout).parts) & SKIP)]

    def example_test(self, checkout, source):
        return Lang.example_test(self, checkout, source)  # tests sit in their own folder, not beside the source

    def test_command(self, pkg_dir, test_path="", extra="", where=None):
        rel = self.within(test_path, pkg_dir) if test_path else self.p.test_glob
        if not rel:
            raise ValueError("no test files to run (the profile lists none)")
        return (self.p.env + self.p.test_cmd.format(package=Path(pkg_dir).name, package_dir=pkg_dir,
                                                    test_path=rel)).rstrip()

    def header(self, text):
        first = re.search(r"^(?:test|describe|it)\(", text, re.M)
        return text[:first.start()] if first else text[:4000]

    def pattern_case(self, text, focus=""):
        """One top-level test(...) block, preferring one that uses the focus's words; a script with none is shown
        whole through its setup (header)."""
        lines, found = text.splitlines(), []
        for i, l in enumerate(lines):
            if re.match(r"(test|it)\(", l):
                for j in range(i + 1, min(len(lines), i + 150)):
                    if lines[j].startswith("});") or lines[j].startswith("})"):
                        found.append("\n".join(lines[i:j + 1]))
                        break
        words = set(re.findall(r"[A-Za-z]{5,}", focus))
        return max(found, key=lambda c: sum(w in c for w in words), default="")

    _TAP = re.compile(r"^\s*(not )?ok \d+ - (.+?)(?:\s+#.*)?$", re.M)

    def cases(self, output):
        """node:test's TAP lines (`ok 1 - name` / `not ok 2 - name`) or its spec reporter (✔ / ✖)."""
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        got = {"passed": [], "failed": []}
        for m in self._TAP.finditer(clean):
            bucket = got["failed"] if m.group(1) else got["passed"]
            if m.group(2) not in bucket:
                bucket.append(m.group(2))
        for sym, bucket in (("✔", got["passed"]), ("✖", got["failed"])):
            for m in re.finditer(rf"^\s*{sym} (.+?)(?: \([\d.]+m?s\))?$", clean, re.M):
                if m.group(1) not in bucket and not m.group(1).startswith("failing tests"):
                    bucket.append(m.group(1))
        return got

    def failure_blocks(self, output):
        """`not ok N - name` → its YAML block (error, expected, actual), up to the next result line."""
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        out = {}
        heads = list(self._TAP.finditer(clean))
        for i, m in enumerate(heads):
            if m.group(1):
                end = heads[i + 1].start() if i + 1 < len(heads) else len(clean)
                out[m.group(2)] = "\n".join(clean[m.end():end].strip("\n").splitlines()[:40])
        return out

    def imports(self, src):
        """Plain-Node repos import across folders with relative paths (`../plugins/mcp-guard/index.js`): those count."""
        out = []
        for m in self._IMPORT.finditer(src):
            if m.group(2).startswith("./"):
                continue
            for part in m.group(1).split(","):
                n = part.strip().split(" as ")[-1].strip()
                if re.fullmatch(r"[A-Za-z_$][\w$]*", n):
                    out.append((n, m.group(2), False))
        return out

    def validate(self, content, rung_name):
        from .testwriter import WriterRefused
        if not re.search(r"""from\s+['"](node:)?assert(/strict)?['"]|require\(\s*['"](node:)?assert""", content):
            raise WriterRefused("the test asserts nothing (no node:assert)")
        if not re.search(r"""from\s+['"]node:test['"]""", content):
            raise WriterRefused("use node:test (import { test } from \"node:test\"), one test() per case")
        if re.search(r"\b(test|it|describe)\.only\(|only:\s*true", content):
            raise WriterRefused(".only would hide other cases")
        if re.search(r"https?://(?!localhost|127\.0\.0\.1|\[::1\]|example\.(?:com|org))[\w.-]+\.\w", content):
            raise WriterRefused("the test reaches for a real host; the sandbox has no network")

    def rules(self) -> str:
        return ("- Write an ES module run with plain `node <file>`: `import { test } from \"node:test\"` and\n"
                "  `import assert from \"node:assert/strict\"`, one top-level test() per case, no nested tests.\n"
                "- Import the code under test with relative paths, the way the example test does: the file is saved\n"
                "  beside it.\n"
                "- No network, no API keys: only servers the test starts itself on localhost, like the example. Setting\n"
                "  a FAKE key in process.env is fine.\n"
                "- Make the failing assertion SHOW the evidence: assert.deepStrictEqual(actual, expected) on the actual\n"
                "  values, never a bare count or boolean, so the failure prints what went wrong.\n"
                "- Do not test anything else. One to three test() cases.")

    def guard_rules(self) -> str:
        return ("Write one top-level test() per trigger, from a list: `for (const c of cases) test(c.name, ...)`, "
                "over every trigger of the class that this code can meet")


def of(profile=None) -> Lang:
    if getattr(profile, "language", "") == "python":
        return Python(profile)
    return NodeScripts(profile) if getattr(profile, "runner", "") == "node" else JS(profile)


def package_map(checkout: Path, profile=None) -> dict:
    """{package name: {"dir": package folder, "deps": names it depends on}} from package.json / pyproject.toml."""
    lang, out = of(profile), {}
    for d in lang.package_dirs(checkout):
        f = Path(checkout) / d / lang.marker
        try:
            if lang.key == "js":
                doc = json.loads(f.read_text())
                name = doc.get("name") or d
                deps = set()
                for k in ("dependencies", "devDependencies", "peerDependencies"):
                    deps |= set((doc.get(k) or {}).keys())
            else:
                import tomllib
                doc = tomllib.loads(f.read_text())
                proj = doc.get("project") or {}
                name = proj.get("name") or ((doc.get("tool") or {}).get("poetry") or {}).get("name") or d
                deps = {re.split(r"[\s<>=!~;\[(]", x.strip(), 1)[0].lower().replace("_", "-")
                        for x in (proj.get("dependencies") or []) if x.strip()}
                name = name.lower().replace("_", "-")
        except Exception:
            continue
        out[name] = {"dir": d, "deps": deps}
    if "." in tuple(lang.p.package_globs) and not any(v["dir"] == "." for v in out.values()):
        out["(repo)"] = {"dir": ".", "deps": set()}  # one package with no manifest at the top: the whole repo
    return out
