"""Run one command in a repo's sandbox, with exactly the pipeline's flags (no env vars, capped, bash -o pipefail).

For the by-hand run, so every command a person runs is one the pipeline could run too.

  uv run python scripts/sandbox_run.py vercel/ai ~/Projects/checkouts/vercel-ai "pnpm --filter ai test:node"
  uv run python scripts/sandbox_run.py vercel/ai <checkout> --net "pnpm install ..."   # network ON (install only)

The profile's environment prefix (pnpm on PATH, caches under /work) is added for you. Prints the output and exits
with the command's own exit code.
"""
import sys
import time
from pathlib import Path

from debug_assist.profiles import PROFILES
from debug_assist.sandbox import run_in_sandbox


def main():
    args = [a for a in sys.argv[1:] if a != "--net"]
    if len(args) != 3 or args[0] not in PROFILES:
        sys.exit(__doc__)
    repo, work, cmd = args
    prof = PROFILES[repo]
    t = time.monotonic()
    r = run_in_sandbox(prof.env + cmd, Path(work).expanduser(), network="--net" in sys.argv, timeout=1800,
                       image=prof.image)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    print(f"\n[sandbox] exit {r.returncode} · {time.monotonic() - t:.0f} s · network {'ON' if '--net' in sys.argv else 'off'}",
          file=sys.stderr)
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
