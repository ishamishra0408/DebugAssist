"""Prepare a repo's BASE checkout: the unmodified code every run copies (checkout.py). Run once per pinned commit.

  uv run python scripts/prepare_base.py vercel/ai            # a new base
  uv run python scripts/prepare_base.py vercel/ai --extend   # install + build what the profile added since
  uv run python scripts/prepare_base.py vercel/ai --source-only --if-missing   # hosted (E2B): the code only; installs
                                                                               # and builds live in the E2B template

Clones exactly the profile's base_commit (shallow), installs with the network ON, then builds with it OFF, all in
the repo's sandbox. Refuses to touch an existing base (delete it yourself if you mean to rebuild it), except
--extend, which only installs and builds into it and must leave the source unmodified.
"""
import subprocess
import sys

from debug_assist.checkout import base_path, check_base
from debug_assist.profiles import PROFILES
from debug_assist.sandbox import run_in_sandbox


def main():
    args = sys.argv[1:]
    extend, source_only, if_missing = "--extend" in args, "--source-only" in args, "--if-missing" in args
    args = [a for a in args if not a.startswith("--")]
    if len(args) != 1 or args[0] not in PROFILES:
        sys.exit(__doc__)
    prof = PROFILES[args[0]]
    b = base_path(prof)
    if extend:
        ok, fact = check_base(prof)
        if not ok:
            sys.exit(f"--extend needs a clean, installed base: {fact}")
    elif b.exists():
        if if_missing:
            print(f"base present: {check_base(prof)[1]}", flush=True)
            return
        sys.exit(f"{b} already exists: {check_base(prof)[1]}")
    else:
        b.mkdir(parents=True)
        for cmd in (["git", "init", "-q"], ["git", "remote", "add", "origin", f"https://github.com/{prof.repo}.git"],
                    ["git", "fetch", "-q", "--depth", "1", "origin", prof.base_commit],
                    ["git", "checkout", "-q", "FETCH_HEAD"]):
            subprocess.run(cmd, cwd=b, check=True)
        (b / ".git" / "info" / "exclude").write_text(".corepack/\n.pnpm-store/\n.bin/\n.da-logs/\n")
    for phase, cmd, net in ([] if source_only else (("install", prof.install_cmd, True), ("build", prof.build_cmd, False))):
        if not cmd:
            continue
        r = run_in_sandbox(prof.env + cmd.format(filters=prof.filters), b, network=net, timeout=1800, image=prof.image)
        print(f"{phase}: exit {r.returncode} (network {'ON' if net else 'off'})")
        if r.returncode != 0:
            sys.exit((r.stdout + r.stderr)[-2000:])
    print(check_base(prof), flush=True)


if __name__ == "__main__":
    main()
