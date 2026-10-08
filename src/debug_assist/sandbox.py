"""Run untrusted repo code in Docker. Guardrail: no secrets inside, no network unless it's the install phase,
capped resources, and a named container that is killed (not orphaned) on timeout.

Docker passes no host environment unless asked (-e / --env-file), and this module never asks. The test suite
checks this by reading the container's environment.

Inside a pipeline step, every command draws its timeout from the run's sandbox-time budget (meter.py: reserved
before it starts, settled after) and writes one event row. Outside a step (tests, preflight) neither applies.
"""
import json
import subprocess
import time
import uuid
from functools import lru_cache
from pathlib import Path

from . import events, meter
from .profiles import PYTHON_IMAGE, RepoProfile

IMAGE = PYTHON_IMAGE
SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASS")


@lru_cache(maxsize=8)
def image_env_names(image: str = IMAGE) -> frozenset:
    """Variables the image itself declares (e.g. python's public GPG_KEY fingerprint). A secret probe must only
    flag variables beyond these: those are the ones that could have come from the host."""
    out = subprocess.run(["docker", "image", "inspect", image, "--format", "{{json .Config.Env}}"],
                         capture_output=True, text=True, check=True, timeout=15).stdout
    return frozenset(e.split("=", 1)[0] for e in json.loads(out) or [])


def run_in_sandbox(command: str, workdir: Path, network: bool = False, timeout: int = 600,
                   image: str = IMAGE) -> subprocess.CompletedProcess:
    from .config import CFG
    if CFG.sandbox_backend == "e2b":  # hosted: the same contract in an E2B sandbox (sandbox_e2b.py)
        from .sandbox_e2b import run
        return run(command, workdir, network=network, timeout=timeout, image=image)
    ctx = events.current()
    if ctx:
        timeout = meter.reserve_seconds(ctx["run_id"], timeout)  # may shrink to what the run has left, or refuse
    name = f"da-{uuid.uuid4().hex[:12]}"
    args = [
        "docker", "run", "--rm", "--name", name,
        "--network", "bridge" if network else "none",
        "--memory", "2g", "--cpus", "2", "--pids-limit", "512",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--tmpfs", "/tmp",
        "-v", f"{Path(workdir).resolve()}:/work", "-w", "/work",
        # pipefail: `tests | tail` must report the TESTS' exit code, not tail's. Caught 2026-10-06: a vercel/ai run
        # with 14 failing tests exited 0 through a pipe, which the ladder would have read as GREEN. bash, because the
        # node image's sh (dash) has no pipefail; an image without bash fails here (closed) rather than hide exit codes.
        image, "bash", "-o", "pipefail", "-c", command,
    ]
    assert not any(a in ("-e", "--env", "--env-file") for a in args), "sandbox must never receive env vars"
    t0 = time.monotonic()
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # killing the docker CLI leaves the container running; kill the container itself
        subprocess.run(["docker", "kill", name], capture_output=True, timeout=30)
        r = subprocess.CompletedProcess(args, 124, "", f"TIMEOUT after {timeout}s; container {name} killed")
    finally:
        if ctx:
            meter.settle_seconds(ctx["run_id"], timeout, time.monotonic() - t0)
    events.log("sandbox", key=name, command=command[:300], image=image.split("@")[0], network=network, timeout_s=timeout,
               exit=r.returncode, seconds=round(time.monotonic() - t0, 1), container=name,
               killed=r.returncode == 124)
    return r


def secrets_visible(workdir: Path, image: str) -> list[str]:
    """Secret-looking env names inside the container that the image itself didn't declare. Works in any image
    (uses `env`, not python). Empty list = clean."""
    from .config import CFG
    if CFG.sandbox_backend == "e2b":
        from .sandbox_e2b import secrets_visible as e2b_secrets
        return e2b_secrets(workdir)
    r = run_in_sandbox("env", workdir, image=image, timeout=60)
    if r.returncode != 0:
        return [f"PROBE_FAILED: {r.stderr.strip()[:200]}"]
    names = {line.split("=", 1)[0] for line in r.stdout.splitlines() if "=" in line}
    return sorted(n for n in names - image_env_names(image) if any(h in n.upper() for h in SECRET_HINTS))


def install_then_test(profile: RepoProfile, workdir: Path, **fmt) -> dict:
    """Phase 1 installs with network; the build (if the profile has one) and the tests run with network OFF."""
    inst = run_in_sandbox(profile.env + profile.install_cmd.format(**fmt), workdir, network=True, image=profile.image)
    if inst.returncode != 0:
        return {"phase": "install", "ok": False, "exit": inst.returncode, "stderr": inst.stderr[-2000:]}
    if profile.build_cmd:
        b = run_in_sandbox(profile.env + profile.build_cmd.format(**fmt), workdir, network=False, image=profile.image)
        if b.returncode != 0:
            return {"phase": "build", "ok": False, "exit": b.returncode, "stdout": b.stdout[-2000:],
                    "stderr": b.stderr[-2000:]}
    test = run_in_sandbox(profile.env + profile.test_cmd.format(**fmt), workdir, network=False, image=profile.image)
    return {"phase": "test", "ok": test.returncode == 0, "exit": test.returncode,
            "stdout": test.stdout[-4000:], "stderr": test.stderr[-2000:]}
