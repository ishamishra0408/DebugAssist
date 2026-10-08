"""Per-run code: one prepared BASE checkout per repo at a pinned commit (installed and built once, never edited), and a
copy-on-write copy of it for each run (APFS clonefile: ~20 s for vercel/ai's 830 MB; only files a run changes take
new disk). A run writes its tests into its own copy, so the base stays clean for the next run.
"""
import os
import subprocess
from pathlib import Path

BASE_DIR = Path(os.getenv("CHECKOUTS_BASE", str(Path.home() / "Projects" / "checkouts" / "base")))


def base_path(profile) -> Path:
    return BASE_DIR / f"{profile.repo.replace('/', '-')}@{profile.base_commit[:7]}"


def check_base(profile) -> tuple[bool, str]:
    """(ok, fact). ok = the base exists, sits at the pinned commit, and nothing in it has been edited."""
    if not profile.base_commit:
        return False, f"{profile.repo} has no pinned base commit in profiles.py"
    b = base_path(profile)
    if not (b / ".git").exists():
        return False, f"no base checkout at {b}"
    head = subprocess.run(["git", "-C", str(b), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if head != profile.base_commit:
        return False, f"base is at {head[:7]}, the profile pins {profile.base_commit[:7]}"
    dirty = subprocess.run(["git", "-C", str(b), "status", "--porcelain"], capture_output=True, text=True).stdout
    if dirty.strip():
        return False, f"base has edits ({len(dirty.splitlines())} files); runs must start from unmodified code"
    from .config import CFG
    if CFG.sandbox_backend == "e2b":  # hosted: the base is source only; installs and builds live in the E2B template
        return True, f"{b.name}, clean, source only (installs and builds are in the E2B template)"
    if not (b / "node_modules").exists() and profile.language == "typescript":
        return False, "base is not installed (no node_modules)"
    return True, f"{b.name}, clean, installed"


def run_copy(profile, dest: Path) -> Path:
    """The run's own copy. Reused as-is on resume (its earlier attempts' files are part of the record)."""
    dest = Path(dest)
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    import sys
    # macOS: an APFS clone (instant, shares disk). Linux hosts: a copy-on-write copy where the filesystem allows it
    flags = ["-cR"] if sys.platform == "darwin" else ["-a", "--reflink=auto"]
    if not base_path(profile).exists():
        ensure_base(profile)
    subprocess.run(["cp", *flags, str(base_path(profile)), str(dest)], check=True, timeout=600)
    mark_template(dest, profile)
    return dest


def mark_template(checkout: Path, profile) -> None:
    """Record which E2B template this code runs in (inside .git, so it never counts as a change)."""
    if getattr(profile, "e2b_template", "") and (Path(checkout) / ".git").exists():
        (Path(checkout) / ".git" / "da-template").write_text(profile.e2b_template)


def ensure_base(profile) -> Path:
    """The source-only base for a hosted run (E2B mode): the repo at its pinned commit, fetched if missing. On the Mac
    the base is installed and built by scripts/prepare_base.py instead."""
    b = base_path(profile)
    if (b / ".git").exists():
        mark_template(b, profile)
        return b
    b.mkdir(parents=True, exist_ok=True)
    for cmd in (["git", "init", "-q"], ["git", "remote", "add", "origin", f"https://github.com/{profile.repo}.git"],
                ["git", "fetch", "-q", "--depth", "1", "origin", profile.base_commit], ["git", "checkout", "-q", "FETCH_HEAD"]):
        subprocess.run(cmd, cwd=b, check=True, timeout=900, capture_output=True)
    (b / ".git" / "info" / "exclude").write_text(".corepack/\n.pnpm-store/\n.bin/\n.da-logs/\n.venv/\n")
    mark_template(b, profile)
    return b
