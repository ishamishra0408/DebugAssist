"""The E2B sandbox keeps the Docker sandbox's contract: the run's exact code, no secrets, no network unless asked,
real exit codes, one event per command. Run against a fake E2B (no account, no network)."""
import dataclasses
import subprocess
from types import SimpleNamespace

import pytest

from debug_assist import sandbox, sandbox_e2b


class Exit(Exception):
    def __init__(self, code, out, err):
        self.exit_code, self.stdout, self.stderr = code, out, err


class Timeout(Exception):
    pass


class FakeSandbox:
    made = []

    def __init__(self, **kw):
        self.kw, self.sandbox_id, self.files_now, self.net, self.killed, self.cmds = kw, f"sbx{len(self.made)}", {}, [], False, []
        self.files = SimpleNamespace(write=self._write, remove=self._remove)
        self.commands = SimpleNamespace(run=self._run)
        FakeSandbox.made.append(self)

    @classmethod
    def create(cls, **kw):
        return cls(**kw)

    def _write(self, path, data, user=None):
        self.files_now[path] = data

    def _remove(self, path, user=None):
        self.files_now[path] = None

    def _run(self, cmd, cwd=None, user=None, timeout=None):
        self.cmds.append((cmd, cwd, timeout))
        if "slow" in cmd:
            raise Timeout()
        if "will-fail" in cmd:
            raise Exit(1, "AssertionError: x\n 1 failed", "")
        return SimpleNamespace(exit_code=0, stdout="ok", stderr="")

    def update_network(self, net):
        self.net.append(net["allow_internet_access"])

    def kill(self):
        self.killed = True


@pytest.fixture
def e2b(monkeypatch, tmp_path):
    FakeSandbox.made = []
    for d in (sandbox_e2b._live, sandbox_e2b._synced, sandbox_e2b._owner):
        d.clear()
    cfg = dataclasses.replace(sandbox_e2b.CFG, sandbox_backend="e2b", e2b_template="debugassist-test")
    monkeypatch.setattr(sandbox_e2b, "CFG", cfg)
    monkeypatch.setattr("debug_assist.config.CFG", cfg)
    monkeypatch.setattr(sandbox_e2b, "_sdk", lambda: (FakeSandbox, Exit, Timeout))
    repo = tmp_path / "co"
    (repo / "packages/p/src").mkdir(parents=True)
    (repo / "packages/p/src/a.ts").write_text("base\n")
    (repo / "packages/p/src/b.ts").write_text("base\n")
    for c in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"]):
        subprocess.run(c, cwd=repo, check=True)
    return repo


def test_the_sandbox_starts_offline_with_no_env_and_runs_the_runs_exact_code(e2b):
    (e2b / "packages/p/src/a.ts").write_text("fixed\n")
    (e2b / "packages/p/src/da-repro-1.test.ts").write_text("test\n")
    r = sandbox.run_in_sandbox("pnpm test", e2b)                        # dispatched by SANDBOX_BACKEND=e2b
    sbx = FakeSandbox.made[0]
    assert sbx.kw["allow_internet_access"] is False and "envs" not in sbx.kw and sbx.kw["template"] == "debugassist-test"
    assert sbx.files_now == {"/work/packages/p/src/a.ts": b"fixed\n", "/work/packages/p/src/da-repro-1.test.ts": b"test\n"}
    assert r.returncode == 0 and sbx.cmds[0][0].startswith("bash -o pipefail -c ") and sbx.cmds[0][1] == "/work"
    # the run puts a.ts back and shelves its draft test: the sandbox follows, in both directions
    subprocess.run(["git", "checkout", "-q", "--", "packages/p/src/a.ts"], cwd=e2b, check=True)
    (e2b / "packages/p/src/da-repro-1.test.ts").unlink()
    sandbox.run_in_sandbox("pnpm test", e2b)
    assert sbx.files_now["/work/packages/p/src/a.ts"] == b"base\n" and sbx.files_now["/work/packages/p/src/da-repro-1.test.ts"] is None
    assert len(FakeSandbox.made) == 1                                     # one sandbox per folder, reused


def test_failures_keep_their_exit_code_and_output(e2b):
    r = sandbox.run_in_sandbox("pnpm test will-fail", e2b)
    assert r.returncode == 1 and "1 failed" in r.stdout


def test_a_timeout_ends_the_sandbox_and_reads_124(e2b):
    r = sandbox.run_in_sandbox("slow", e2b, timeout=5)
    assert r.returncode == 124 and FakeSandbox.made[0].killed and not sandbox_e2b._live


def test_the_network_is_on_only_for_the_command_that_asks(e2b):
    sandbox.run_in_sandbox("pnpm install", e2b, network=True)
    assert FakeSandbox.made[0].net == [True, False]


def test_no_template_is_a_named_failure_not_a_pass(e2b, monkeypatch):
    monkeypatch.setattr(sandbox_e2b, "CFG", dataclasses.replace(sandbox_e2b.CFG, e2b_template=""))
    r = sandbox.run_in_sandbox("pnpm test", e2b)
    assert r.returncode == 125 and "E2B_TEMPLATE is not set" in r.stderr


def test_the_secret_probe_flags_only_what_the_template_did_not_declare(e2b, monkeypatch):
    out = "PATH=/x\nNODE_VERSION=22\nLEAKED_API_KEY=x\n---BASELINE---\nNODE_VERSION\nPATH\n"
    monkeypatch.setattr(sandbox_e2b, "run", lambda *a, **k: subprocess.CompletedProcess("env", 0, out, ""))
    assert sandbox_e2b.secrets_visible(e2b) == ["LEAKED_API_KEY"]


def test_a_runs_sandboxes_end_together(e2b, monkeypatch):
    sandbox_e2b._sandbox(e2b, "run-1")
    assert sandbox_e2b.close_run("run-1") == 1 and FakeSandbox.made[0].killed


def test_each_repo_runs_in_its_own_template(e2b):
    (e2b / ".git" / "da-template").write_text("debugassist-acme-widgets-1234567\n")
    sandbox.run_in_sandbox("pytest", e2b)
    assert FakeSandbox.made[0].kw["template"] == "debugassist-acme-widgets-1234567"
