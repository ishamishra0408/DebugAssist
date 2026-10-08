"""One profile per repo: which sandbox image, how to install, how to test, and where its code and tests live.

Two kinds: BUILT-IN (vercel/ai, set up by hand on 2026-10-06) and CONNECTED (written by the connect-a-repo pipeline,
connect.py, and stored in MongoDB's `repos` collection). get(repo) finds either; built-ins win.

Two phases (ruled 2026-10-06): INSTALL runs with network (package registries); BUILD and TEST run with network OFF.
Images are pinned by digest so a new upstream release can't change behaviour mid-week.
Install/test commands are starting points; Friday's by-hand run refines them per issue.
"""
import os
from dataclasses import dataclass, fields

PYTHON_IMAGE = "python@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f"  # python:3.12-slim
NODE_IMAGE = "node@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392"      # node:22-slim


@dataclass(frozen=True)
class RepoProfile:
    repo: str
    language: str
    image: str
    install_cmd: str   # phase 1: network ON
    test_cmd: str      # phase 2: network OFF
    build_cmd: str = ""  # between them, network OFF (monorepos whose tests import sibling packages' builds)
    env: str = ""        # shell prefix for every command in this repo's sandbox (never host secrets)
    recorded_fixtures: bool = False  # the repo ships recorded streams the ladder's integration rung can cut
    base_commit: str = ""            # the pinned commit runs start from (checkout.py), unmodified
    filters: str = ""                # which packages to install and build (monorepos), for {filters}
    # added for connect-a-repo (2026-10-08); the defaults describe vercel/ai, so the built-in profile is unchanged
    e2b_template: str = ""           # the E2B template hosted runs start from (built by connect.py or scripts/e2b_template.py)
    default_branch: str = "main"
    manager: str = "pnpm"            # pnpm | npm | yarn | uv | poetry | pip
    runner: str = "vitest"           # vitest | jest | pytest
    package_globs: tuple = ("packages/*",)          # where packages live (a single-package repo: ("."))
    source_globs: tuple = ("packages/*/src/**",)    # where source files live (what Gather context searches)
    test_style: str = "beside"       # new tests go "beside" the source, or in the package's "tests" folder
    test_suffix: str = ".test.ts"    # how a test file is named: a suffix (".test.ts") or a prefix pattern ("test_*.py")
    source: str = "built-in"         # built-in | connected
    connected_at: str = ""
    baseline: tuple = ()             # (package dir, "pass" | "fail") for each suite run at connection, network off
    notes: tuple = ()                # what the connection found and why it chose each setting
    test_glob: str = ""              # plain-Node repos (runner "node"): the test files that make up the suite


# vercel/ai, proven 2026-10-06 at e7f55a4 in NODE_IMAGE: install 55 s, build 63 s, then provider-utils 1,064, gateway 645
# and ai 4,252 tests all pass offline. Everything pnpm needs lives under /work, because each command is a fresh container:
# corepack's pnpm (COREPACK_HOME), a `pnpm` on PATH (package scripts call it by name), and the package store.
# pnpm_config_verify_deps_before_run=error: caught 2026-10-06, pnpm re-installs before any `pnpm run` when node_modules is
# out of sync, and with the network off that retries for 10+ minutes instead of failing. `error` fails at once, by name.
_PNPM = ("export COREPACK_HOME=/work/.corepack COREPACK_ENABLE_DOWNLOAD_PROMPT=0 CI=1 NO_COLOR=1 PATH=/work/.bin:$PATH"
         " pnpm_config_verify_deps_before_run=error"
         " && mkdir -p /work/.bin && corepack enable --install-directory /work/.bin pnpm && ")


PROFILES = {
    "vercel/ai": RepoProfile(
        "vercel/ai", "typescript", NODE_IMAGE,
        e2b_template=os.getenv("E2B_TEMPLATE", "debugassist-vercel-ai-e7f55a4"),
        install_cmd="pnpm install --frozen-lockfile --store-dir /work/.pnpm-store {filters}",
        build_cmd="pnpm {filters} build",
        test_cmd="cd packages/{package} && pnpm test:node {test_path}",
        env=_PNPM,
        recorded_fixtures=True,
        base_commit="e7f55a481fe2c39bd2540162ee8b45bd8de3354b",  # main on 2026-10-07; baseline green offline
        # ai, gateway and every provider using the streaming tool-call tracker (the #21439 blast radius)
        filters=" ".join(f"--filter '{p}...'" for p in ("ai", "@ai-sdk/gateway", "@ai-sdk/openai-compatible",
                         "@ai-sdk/openai", "@ai-sdk/groq", "@ai-sdk/deepseek", "@ai-sdk/alibaba", "@ai-sdk/mistral",
                         "@ai-sdk/moonshotai", "@ai-sdk/xai",
                         # Ruled 2026-10-07: install the fixed package's dependents, so "changed packages + installed
                         # dependents stay green" (north-star-v1.1) runs them. Both Opus fixes changed openai-compatible;
                         # these 9 depend on it directly. Dependents still not installed are listed on every fix.
                         "@ai-sdk/baseten", "@ai-sdk/cerebras", "@ai-sdk/deepinfra", "@ai-sdk/fireworks",
                         "@ai-sdk/gmicloud", "@ai-sdk/google-vertex", "@ai-sdk/huggingface", "@ai-sdk/togetherai",
                         "@ai-sdk/zai",
                         # 2026-10-08, run of #22085: the issue was in @ai-sdk/workflow, which was not installed, so
                         # no test in it could load (missing @vercel/ai-tsconfig) and 4 tries were spent for nothing
                         "@ai-sdk/workflow")),  # e.g. packages/openai-compatible/src/chat/__fixtures__/*.chunks.txt (by-hand run, rung 2)
    ),
    "langchain-ai/langchain": RepoProfile(
        "langchain-ai/langchain", "python", PYTHON_IMAGE,
        install_cmd="pip install -q uv && cd {package_dir} && uv sync --group test",
        test_cmd="cd {package_dir} && uv run --no-sync pytest -q {test_path}",
    ),
    "Arize-ai/phoenix": RepoProfile(
        "Arize-ai/phoenix", "python", PYTHON_IMAGE,
        install_cmd="pip install -q uv && uv sync",
        test_cmd="uv run --no-sync pytest -q {test_path}",
    ),
}


class UnknownRepo(KeyError):
    pass


def to_doc(p: RepoProfile) -> dict:
    return {f.name: (list(getattr(p, f.name)) if isinstance(getattr(p, f.name), tuple) else getattr(p, f.name))
            for f in fields(RepoProfile)}


def from_doc(d: dict) -> RepoProfile:
    names = {f.name for f in fields(RepoProfile)}
    return RepoProfile(**{k: (tuple(tuple(x) if isinstance(x, list) else x for x in v) if isinstance(v, list) else v)
                          for k, v in d.items() if k in names})


def connected(repo: str) -> RepoProfile | None:
    """A repo the connect-a-repo pipeline set up, from MongoDB; None when there is none (or no database)."""
    try:
        from .config import CFG
        from .store import client, reachable
        if not reachable():
            return None
        doc = client(1500)[CFG.db_name]["repos"].find_one({"_id": repo, "status": "connected"}, {"profile": 1})
    except Exception:
        return None
    return from_doc(doc["profile"]) if doc else None


def get(repo: str) -> RepoProfile:
    """The profile for owner/name: built-in first, then connected."""
    if repo in PROFILES and PROFILES[repo].base_commit:
        return PROFILES[repo]
    p = connected(repo)
    if p:
        return p
    if repo in PROFILES:
        return PROFILES[repo]
    raise UnknownRepo(f"{repo} is not connected yet. Connect it first (the Connect a repo page).")


def ready() -> list[str]:
    """Repos a run can start on: built-ins with a pinned commit, plus every connected repo."""
    out = [k for k, p in PROFILES.items() if p.base_commit]
    try:
        from .config import CFG
        from .store import client, reachable
        if reachable():
            out += [d["_id"] for d in client(1500)[CFG.db_name]["repos"].find({"status": "connected"}, {"_id": 1})
                    if d["_id"] not in out]
    except Exception:
        pass
    return out


def profile_for(owner: str, repo: str) -> RepoProfile:
    return get(f"{owner}/{repo}")
