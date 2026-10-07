"""Each guardrail is tested on what it must REFUSE, not only on the happy path."""
import os
import shutil
import subprocess
import time

import pytest

from debug_assist import budget
from debug_assist.guardrails import (GuardrailViolation, assert_no_names, find_names, freeze_condition, now,
                                     record_approval, verify_approval, verify_condition_frozen)


class FakeStore:
    def __init__(self):
        self.docs = []

    def insert_one(self, doc):
        self.docs.append(doc)

    def find_one(self, q):
        return next((d for d in self.docs if all(d.get(k) == v for k, v in q.items())), None)


# 1 ── approval attestation ────────────────────────────────────────────────────────────────────
def test_publish_refused_without_approval():
    with pytest.raises(GuardrailViolation, match="no approval"):
        verify_approval(FakeStore(), "r1", "body")


def test_publish_refused_if_text_changed_after_approval():
    st = FakeStore()
    record_approval(st, "r1", "approved text", "isha")
    with pytest.raises(GuardrailViolation, match="changed after approval"):
        verify_approval(st, "r1", "approved text + a sneaky extra line")


def test_publish_allowed_for_exact_approved_text():
    st = FakeStore()
    record_approval(st, "r1", "approved text", "isha")
    assert verify_approval(st, "r1", "approved text")["approver"] == "isha"


# 2 ── no names in public output ───────────────────────────────────────────────────────────────
def test_handles_and_known_names_are_caught():
    text = "This shipped because @jdoe skipped the review, and Haaaarry approved it."
    assert find_names(text, ["Haaaarry"]) == ["@jdoe", "Haaaarry"]
    with pytest.raises(GuardrailViolation):
        assert_no_names(text, ["Haaaarry"])


def test_condition_language_passes():
    assert_no_names("No test sends a trailing-slash path, and paths are not normalised.", ["Haaaarry"])


def test_email_and_decorators_are_not_handles():
    assert find_names("contact a@b.com; use @pytest.mark.parametrize", []) == []
    assert find_names("```python\n@property\ndef x(self): ...\n```", []) == []


def test_handle_at_sentence_end_is_still_caught():
    assert find_names("This was approved by @jdoe.", []) == ["@jdoe"]


def test_known_name_inside_code_is_still_caught():
    assert find_names("```\n# author: Haaaarry\n```", ["Haaaarry"]) == ["Haaaarry"]


# 3 ── condition frozen before the guard ───────────────────────────────────────────────────────
def test_guard_written_before_freeze_is_refused():
    st = FakeStore()
    guard_time = now()
    time.sleep(0.01)
    freeze_condition(st, "r1", "cond")
    with pytest.raises(GuardrailViolation, match="before the condition was frozen"):
        verify_condition_frozen(st, "r1", "cond", guard_time)


def test_condition_edited_after_freeze_is_refused():
    st = FakeStore()
    freeze_condition(st, "r1", "cond")
    with pytest.raises(GuardrailViolation, match="changed after it was frozen"):
        verify_condition_frozen(st, "r1", "cond, reworded to suit the guard", now())


def test_condition_cannot_be_refrozen():
    st = FakeStore()
    freeze_condition(st, "r1", "cond")
    with pytest.raises(GuardrailViolation, match="already frozen"):
        freeze_condition(st, "r1", "a different cond")


def test_refreezing_the_same_condition_on_resume_is_a_no_op():
    st = FakeStore()
    first = freeze_condition(st, "r1", "cond")
    time.sleep(0.01)
    again = freeze_condition(st, "r1", "cond")  # the step re-runs after a crash
    assert again["frozen_at"] == first["frozen_at"] and len(st.docs) == 1


# 4 ── spend cap and turn caps (the MongoDB-backed parts are in test_meter.py) ─────────────────
def test_unpriced_model_is_refused():
    with pytest.raises(budget.BudgetExceeded, match="no price on file"):
        budget.cost_usd("some/new-model", {"input_tokens": 1, "output_tokens": 1})


def test_cached_tokens_priced_at_cache_rate():
    u = {"input_tokens": 1_000_000, "output_tokens": 0, "input_token_details": {"cache_read": 1_000_000}}
    assert budget.cost_usd("anthropic/claude-opus-5.5", u) == pytest.approx(0.20)


# 5 ── no secrets in the sandbox ───────────────────────────────────────────────────────────────
docker_up = shutil.which("docker") and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.mark.skipif(not docker_up, reason="Docker not running")
def test_sandbox_sees_no_host_secrets(tmp_path, monkeypatch):
    from debug_assist.sandbox import run_in_sandbox
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-canary-should-never-leak")
    r = run_in_sandbox("env; echo; cat /proc/self/environ 2>/dev/null | tr '\\0' '\\n'", tmp_path)
    assert r.returncode == 0, r.stderr
    assert "sk-canary-should-never-leak" not in r.stdout
    assert "OPENROUTER" not in r.stdout and "GITHUB_TOKEN" not in r.stdout


@pytest.mark.skipif(not docker_up, reason="Docker not running")
def test_sandbox_has_no_network(tmp_path):
    from debug_assist.sandbox import run_in_sandbox
    r = run_in_sandbox("python -c \"import urllib.request; urllib.request.urlopen('https://pypi.org', timeout=5)\"",
                       tmp_path)
    assert r.returncode != 0, "sandbox reached the internet with network disabled"


@pytest.mark.skipif(not docker_up, reason="Docker not running")
def test_a_pipe_cannot_hide_a_failing_command(tmp_path):
    """`failing tests | tail` must still fail, or the ladder would read a red test as GREEN."""
    from debug_assist.profiles import NODE_IMAGE
    from debug_assist.sandbox import run_in_sandbox
    assert run_in_sandbox("false | cat", tmp_path).returncode != 0
    assert run_in_sandbox("false | tail -1", tmp_path, image=NODE_IMAGE).returncode != 0
