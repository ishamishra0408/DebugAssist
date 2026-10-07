"""Layer 0 preflight: standard library only, so it still runs when `import debug_assist` itself is broken.

Checks the one failure the main preflight can't see: macOS has hidden the environment's .pth files (iCloud did this
on 2026-10-06), so Python silently skips them and the package "disappears".

Run:  python3 scripts/doctor.py          (add --fix to unhide)
"""
import glob
import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pths = glob.glob(str(ROOT / ".venv" / "lib" / "python3.*" / "site-packages" / "*.pth"))
hidden = [p for p in pths if os.stat(p).st_flags & stat.UF_HIDDEN]
fix = "--fix" in sys.argv
if hidden and fix:
    for p in hidden:
        os.chflags(p, os.stat(p).st_flags & ~stat.UF_HIDDEN)
    hidden = []
if hidden:
    print(f"DOCTOR FAIL  {len(hidden)} hidden .pth file(s): Python skips them, so imports vanish")
    print("             fix: python3 scripts/doctor.py --fix")
    sys.exit(1)
venv_py = ROOT / ".venv" / "bin" / "python"
r = subprocess.run([str(venv_py), "-c", "import debug_assist"], capture_output=True, text=True)
if r.returncode != 0:
    print("DOCTOR FAIL  the package doesn't import:", r.stderr.strip().splitlines()[-1] if r.stderr else "?")
    print("             fix: uv sync")
    sys.exit(1)
print(f"DOCTOR PASS  {len(pths)} .pth files visible; package imports")
