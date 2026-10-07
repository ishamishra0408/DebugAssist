"""One profile per repo: which sandbox image, how to install, how to test.

Two phases (ruled 2026-10-06): INSTALL runs with network (package registries), TEST runs with network OFF.
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


PROFILES = {
    "vercel/ai": RepoProfile(
        "vercel/ai", "typescript", NODE_IMAGE,
        install_cmd="corepack enable && pnpm install --frozen-lockfile",
        test_cmd="pnpm --filter {package} test",
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
