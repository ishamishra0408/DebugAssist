"""One profile per repo: which sandbox image, how to install, how to test.

Two phases (ruled 2026-10-06): INSTALL runs with network (package registries); BUILD and TEST run with network OFF.
Images are pinned by digest so a new upstream release can't change behaviour mid-week.
Install/test commands are starting points; Friday's by-hand run refines them per issue.
"""
from dataclasses import dataclass

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
                         "@ai-sdk/zai")),  # e.g. packages/openai-compatible/src/chat/__fixtures__/*.chunks.txt (by-hand run, rung 2)
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


def profile_for(owner: str, repo: str) -> RepoProfile:
    key = f"{owner}/{repo}"
    if key not in PROFILES:
        raise UnknownRepo(f"no sandbox profile for {key}; add one to profiles.py (image, install, test)")
    return PROFILES[key]
