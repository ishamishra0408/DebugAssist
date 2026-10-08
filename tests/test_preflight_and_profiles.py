"""Preflight must refuse with EVERY named reason at once; profiles must route each repo to the right sandbox."""
import shutil
import subprocess

import pytest

from debug_assist import budget, preflight
from debug_assist.preflight import Check, report
from debug_assist.profiles import NODE_IMAGE, PYTHON_IMAGE, UnknownRepo, profile_for


def test_report_refuses_and_lists_every_failure():
    checks = [Check("Docker", "PASS", "up"), Check("MongoDB", "FAIL", "not primary", "fix A"),
              Check("Phoenix", "FAIL", "down", "fix B"), Check("Vector index", "WARN", "no index yet", "later")]
    ok, text = report(checks)
    assert not ok
    assert "MongoDB" in text and "Phoenix" in text and "fix A" in text and "fix B" in text
    assert "REFUSED: 2 problem(s)" in text


def test_report_passes_with_warnings_only():
    ok, text = report([Check("Phoenix", "WARN", "down, --no-trace"), Check("Docker", "PASS", "up")])
    assert ok and "nothing was started" in text
    assert "starting the run" in report([Check("Docker", "PASS", "up")], starting=True)[1]


def test_phoenix_down_refuses_unless_no_trace(monkeypatch):
    monkeypatch.setattr(preflight, "_http", lambda *a, **k: (None, "refused", {}))
    assert preflight.check_phoenix(trace=True, boot_wait_s=0).status == "FAIL"
    assert preflight.check_phoenix(trace=False, boot_wait_s=0).status == "WARN"


def test_classic_github_token_is_refused(monkeypatch):
    import dataclasses
    monkeypatch.setattr(preflight, "CFG", dataclasses.replace(preflight.CFG, github_token="ghp_classicBroadScopes"))
    c = preflight.check_github("vercel", "ai")
    assert c.status == "FAIL" and "fine-grained" in c.fact


def test_profiles_route_by_language():
    assert profile_for("vercel", "ai").image == NODE_IMAGE
    assert profile_for("langchain-ai", "langchain").image == PYTHON_IMAGE
    with pytest.raises(UnknownRepo, match="no sandbox profile"):
        profile_for("someone", "unknown")


def test_demo_runs_get_the_demo_cap():
    assert budget.run_cap({"demo": True}) == 2.50
    assert budget.run_cap({}) == 0.50


docker_up = shutil.which("docker") and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.mark.skipif(not docker_up, reason="Docker not running")
def test_node_sandbox_sees_no_host_secrets(tmp_path, monkeypatch):
    from debug_assist.sandbox import secrets_visible
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-canary-should-never-leak")
    assert secrets_visible(tmp_path, NODE_IMAGE) == []


def test_vercel_sandbox_fails_fast_instead_of_hanging():
    """pnpm re-installs before a run when node_modules is out of sync; offline that retried for 10+ minutes
    (caught 2026-10-06). The profile makes it fail at once, by name."""
    env = profile_for("vercel", "ai").env
    assert "pnpm_config_verify_deps_before_run=error" in env
    assert "COREPACK_HOME=/work/" in env  # fresh container per command: caches must live in the mounted checkout


def test_phoenix_cloud_check_tests_the_address_and_key_together(monkeypatch):
    import dataclasses
    import io
    import urllib.error
    import urllib.request
    from debug_assist import preflight
    monkeypatch.setattr(preflight, "CFG", dataclasses.replace(preflight.CFG, phoenix_endpoint="https://app.phoenix.arize.com/s/space"))
    monkeypatch.setenv("PHOENIX_API_KEY", "k")
    seen = {}

    def answer(code):
        def fake(req, timeout=None):
            seen.update(url=req.full_url, auth=req.headers.get("Authorization"))
            if code >= 400:
                raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO())
            return type("R", (io.BytesIO,), {"status": code, "__enter__": lambda s: s, "__exit__": lambda *a: False})()
        return fake
    monkeypatch.setattr(urllib.request, "urlopen", answer(200))
    assert preflight.check_phoenix_cloud(True).status == "PASS"
    assert seen == {"url": "https://app.phoenix.arize.com/s/space/v1/traces", "auth": "Bearer k"}
    monkeypatch.setattr(urllib.request, "urlopen", answer(401))
    c = preflight.check_phoenix_cloud(True)
    assert c.status == "FAIL" and "refuses the key" in c.fact
    monkeypatch.setattr(urllib.request, "urlopen", answer(404))
    assert "not a traces address" in preflight.check_phoenix_cloud(True).fact
