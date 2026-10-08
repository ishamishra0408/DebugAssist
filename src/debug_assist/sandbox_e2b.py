"""E2B: the sandbox for hosted runs (Render cannot run Docker). Same contract as the Docker sandbox (sandbox.py): no
secrets inside, no network unless a command asks for it, a time cap per command drawn from the run's sandbox-time
budget, and one event per command.

One E2B sandbox per working folder of a run (the run's code copy, the unfixed copy for the second test, ...), started
from a template that already holds the repo at the pinned commit, installed and built (scripts/e2b_template.py). Before
each command, the files the run changed in that folder (git status against the pinned commit) are written into the
sandbox, and files it put back are restored, so the sandbox runs exactly the run's code. Nothing comes back except the
exit code and the output.

The sandbox is created with internet access off and receives no environment variables: the E2B key stays in this
process. A command that asks for the network (installs only) gets it for that command and loses it after.
"""
import hashlib
import shlex
import subprocess
import time
import uuid
from pathlib import Path

from . import events, meter
from .config import CFG

WORK = "/work"
USER = "root"          # inside its own VM; the template's /work belongs to root
_live: dict = {}       # resolved workdir → Sandbox (this process)
_synced: dict = {}     # resolved workdir → {repo path: sha1 of what was last written}
_owner: dict = {}      # resolved workdir → run id, so a run's sandboxes can be closed together


class SandboxUnavailable(RuntimeError):
    pass


def _sdk():
    from e2b import CommandExitException, Sandbox, TimeoutException
    return Sandbox, CommandExitException, TimeoutException


def _sandbox(workdir: Path, run_id: str | None):
    key = str(Path(workdir).resolve())
    if key in _live:
        return _live[key]
    template = template_for(workdir)
    if not template:
        raise SandboxUnavailable("E2B_TEMPLATE is not set (build it with scripts/e2b_template.py)")
    Sandbox, _, _ = _sdk()
    sbx = Sandbox.create(template=template, timeout=3600, allow_internet_access=False,
                         metadata={"run": run_id or "", "dir": Path(workdir).name})
    _live[key], _synced[key], _owner[key] = sbx, {}, run_id
    return sbx


def template_for(workdir: Path) -> str:
    """The template this code runs in: written into .git/da-template when the code copy was made (each connected repo
    has its own); the global E2B_TEMPLATE otherwise."""
    f = Path(workdir) / ".git" / "da-template"
    return f.read_text().strip() if f.exists() else CFG.e2b_template


def changed_files(workdir: Path) -> set[str]:
    """Repo paths that differ from the pinned commit (modified, added, untracked or deleted), .gitignore respected."""
    out = subprocess.run(["git", "-C", str(workdir), "status", "--porcelain", "--untracked-files=all", "-z"],
                         capture_output=True, text=True, timeout=60).stdout
    paths, entries = set(), out.split("\0")
    i = 0
    while i < len(entries):
        e = entries[i]
        if len(e) > 3:
            paths.add(e[3:])
            if e[0] in "RC":  # a rename carries its old path as the next entry: both differ from the commit
                i += 1
                if i < len(entries) and entries[i]:
                    paths.add(entries[i])
        i += 1
    return paths


def sync(sbx, workdir: Path) -> int:
    """Make the sandbox's /work match the run's folder for every path that differs now or differed before."""
    key = str(Path(workdir).resolve())
    prev, wrote = _synced.setdefault(key, {}), 0
    for p in sorted(changed_files(workdir) | set(prev)):
        f = Path(workdir) / p
        if f.is_file():
            data = f.read_bytes()
            h = hashlib.sha1(data).hexdigest()
            if prev.get(p) != h:
                sbx.files.write(f"{WORK}/{p}", data, user=USER)
                prev[p], wrote = h, wrote + 1
        elif p in prev or not f.exists():
            try:
                sbx.files.remove(f"{WORK}/{p}", user=USER)
            except Exception:  # already absent in the sandbox
                pass
            prev.pop(p, None)
            wrote += 1
    return wrote


def run(command: str, workdir: Path, network: bool = False, timeout: int = 600, image: str = "") -> subprocess.CompletedProcess:
    ctx = events.current()
    if ctx:
        timeout = meter.reserve_seconds(ctx["run_id"], timeout)
    name, sbx_id, t0 = f"e2b-{uuid.uuid4().hex[:12]}", None, time.monotonic()
    try:
        _, CommandExitException, TimeoutException = _sdk()
        sbx = _sandbox(workdir, (ctx or {}).get("run_id"))
        sbx_id = sbx.sandbox_id
        sync(sbx, workdir)
        if network:
            sbx.update_network({"allow_internet_access": True})
        try:
            # pipefail, as in the Docker sandbox: `tests | tail` must report the tests' exit code
            res = sbx.commands.run(f"bash -o pipefail -c {shlex.quote(command)}", cwd=WORK, user=USER, timeout=timeout)
            r = subprocess.CompletedProcess(command, res.exit_code, res.stdout, res.stderr)
        except CommandExitException as e:
            r = subprocess.CompletedProcess(command, e.exit_code, e.stdout, e.stderr)
        except TimeoutException:
            close(workdir)  # the command may still be running inside: end the sandbox; the next command starts afresh
            r = subprocess.CompletedProcess(command, 124, "", f"TIMEOUT after {timeout}s; sandbox {sbx_id} ended")
        finally:
            if network and str(Path(workdir).resolve()) in _live:
                sbx.update_network({"allow_internet_access": False})
    except Exception as e:  # no key, no template, E2B down: a named failure, never a hidden pass
        r = subprocess.CompletedProcess(command, 125, "", f"SANDBOX UNAVAILABLE ({type(e).__name__}): {str(e)[:300]}")
    finally:
        if ctx:
            meter.settle_seconds(ctx["run_id"], timeout, time.monotonic() - t0)
    events.log("sandbox", key=name, command=command[:300], image=f"e2b:{template_for(workdir)}", network=network,
               timeout_s=timeout, exit=r.returncode, seconds=round(time.monotonic() - t0, 1), container=sbx_id or name,
               killed=r.returncode == 124, backend="e2b")
    return r


def close(workdir: Path) -> None:
    key = str(Path(workdir).resolve())
    sbx = _live.pop(key, None)
    _synced.pop(key, None)
    _owner.pop(key, None)
    if sbx is not None:
        try:
            sbx.kill()
        except Exception:
            pass


def close_run(run_id: str) -> int:
    """End every sandbox a run started (when it pauses, stops or finishes). E2B also ends them after an hour."""
    keys = [k for k, owner in _owner.items() if owner == run_id]
    for k in keys:
        close(Path(k))
    return len(keys)


def secrets_visible(workdir: Path) -> list[str]:
    """Secret-looking env names inside the sandbox beyond the template's own (recorded at build in
    /etc/da-env-baseline). Empty list = clean; a failed probe fails closed."""
    r = run("env; echo ---BASELINE---; cat /etc/da-env-baseline", workdir, timeout=60)
    if r.returncode != 0 or "---BASELINE---" not in r.stdout:
        return [f"PROBE_FAILED: {(r.stderr or r.stdout).strip()[:200]}"]
    env_part, base_part = r.stdout.split("---BASELINE---", 1)
    names = {line.split("=", 1)[0] for line in env_part.splitlines() if "=" in line}
    baseline = {line.strip() for line in base_part.splitlines() if line.strip()}
    return sorted(n for n in names - baseline if any(h in n.upper() for h in ("KEY", "TOKEN", "SECRET", "PASS")))
