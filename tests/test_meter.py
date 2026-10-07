"""The spend stop must hold across a crash: checked BEFORE each call, counted in MongoDB, never in the run state."""
import pytest

from debug_assist import budget, events, meter, models
from debug_assist.budget import BudgetExceeded, TurnCapExceeded
from debug_assist.meter import SandboxTimeExceeded

OPUS = "anthropic/claude-opus-5.5"


def test_a_call_that_could_cross_the_cap_is_refused_before_it_is_made(scratch_db):
    meter.open_run("r1", cap_usd=0.50, sandbox_cap_s=600)
    meter.reserve_call("r1", "find_cause", OPUS, worst_usd=0.30, max_tokens=4096)
    with pytest.raises(BudgetExceeded, match="Refused BEFORE calling"):
        meter.reserve_call("r1", "find_cause", OPUS, worst_usd=0.30, max_tokens=4096)  # 0.30 + 0.30 > 0.50


def test_a_crash_between_reserve_and_settle_keeps_the_money_counted(scratch_db):
    meter.open_run("r1", 0.50, 600)
    meter.reserve_call("r1", "find_cause", OPUS, 0.40, 4096)  # ...process dies here, nothing settled
    # the resumed process knows nothing from the run state; MongoDB still holds the reservation
    assert meter.snapshot("r1")["reserved_usd"] == pytest.approx(0.40)
    with pytest.raises(BudgetExceeded):
        meter.reserve_call("r1", "find_cause", OPUS, 0.20, 4096)
    stuck = scratch_db["calls"].find_one({"run_id": "r1"})
    assert stuck["status"] == "reserved"  # the crash leaves a visible trace


def test_reopening_on_resume_does_not_reset_spend_or_caps(scratch_db):
    meter.open_run("r1", 0.50, 600)
    c = meter.reserve_call("r1", "find_cause", OPUS, 0.10, 100)
    meter.settle_call(c, {"input_tokens": 1, "output_tokens": 1}, 0.07, ms=5)
    meter.open_run("r1", 99.0, 99_999)  # resume calls open again, even with other caps
    s = meter.snapshot("r1")
    assert s["spent_usd"] == pytest.approx(0.07) and s["cap_usd"] == 0.50 and s["sandbox_cap_s"] == 600


def test_micro_dollars_add_up_exactly(scratch_db):
    meter.open_run("r1", 1.0, 600)
    for _ in range(200):
        c = meter.reserve_call("r1", "find_cause", "qwen/qwen3-coder-next", 0.000004, 16)
        meter.settle_call(c, {}, 0.000004, ms=1)
    s = meter.snapshot("r1")
    assert s["spent_usd"] == pytest.approx(0.0008) and s["reserved_usd"] == 0


def test_turn_caps_live_in_mongodb_and_survive_a_rerun_step(scratch_db):
    meter.open_run("r1", 1.0, 600)
    for _ in range(2):
        meter.take_turn("r1", "why_it_shipped")  # cap 2
    with pytest.raises(TurnCapExceeded, match="refused before calling"):
        meter.take_turn("r1", "why_it_shipped")  # the step re-runs after a crash: its turns are not given back


def test_unmetered_runs_are_refused(scratch_db):
    with pytest.raises(BudgetExceeded, match="no meter"):
        meter.reserve_call("never-opened", "find_cause", OPUS, 0.01, 10)
    with pytest.raises(SandboxTimeExceeded, match="no meter"):
        meter.reserve_seconds("never-opened", 60)


def test_sandbox_time_is_capped_per_run(scratch_db):
    meter.open_run("r1", 1.0, sandbox_cap_s=700)
    g = meter.reserve_seconds("r1", 600)
    meter.settle_seconds("r1", g, used_s=590.2)            # 591 s used
    assert meter.reserve_seconds("r1", 600) == 109           # the last command gets what is left
    with pytest.raises(SandboxTimeExceeded, match="cap reached"):
        meter.reserve_seconds("r1", 600)                     # 0 left while that one is out


def test_worst_case_bounds_the_real_cost():
    msgs = [("system", "You fix bugs."), ("user", "x" * 4000)]
    worst = budget.worst_case_usd(OPUS, msgs, max_tokens=1000)
    real = budget.cost_usd(OPUS, {"input_tokens": 1100, "output_tokens": 1000})  # ~4 chars per token in practice
    assert worst >= real
    with pytest.raises(BudgetExceeded, match="no price on file"):
        budget.worst_case_usd("some/new-model", msgs, 10)


# ── write(): the only door to a generation model ─────────────────────────────────────────────────
class FakeLLM:
    def __init__(self, fail=False):
        self.calls, self.fail = 0, fail

    def invoke(self, messages):
        self.calls += 1
        if self.fail:
            raise TimeoutError("provider timed out")
        from langchain_core.messages import AIMessage
        return AIMessage("OK", usage_metadata={"input_tokens": 20, "output_tokens": 2, "total_tokens": 22})


def test_write_refuses_before_calling_when_over_budget(scratch_db, monkeypatch):
    llm = FakeLLM()
    monkeypatch.setattr(models, "_writer", lambda demo, max_tokens: (OPUS, llm))
    meter.open_run("r1", cap_usd=0.01, sandbox_cap_s=600)
    with events.bind("r1", "find_cause"), pytest.raises(BudgetExceeded):
        models.write({"run_id": "r1"}, "find_cause", [("user", "ping")], max_tokens=4096)  # worst $0.08 > $0.01
    assert llm.calls == 0, "the model was called although the worst case could pass the cap"


def test_write_settles_the_real_cost_and_logs_one_event(scratch_db, monkeypatch):
    monkeypatch.setattr(models, "_writer", lambda demo, max_tokens: (OPUS, FakeLLM()))
    meter.open_run("r1", 0.50, 600)
    with events.bind("r1", "find_cause"):
        msg, upd = models.write({"run_id": "r1"}, "find_cause", [("user", "ping")], max_tokens=16)
    assert upd["spent_usd"] == pytest.approx(budget.cost_usd(OPUS, {"input_tokens": 20, "output_tokens": 2}), abs=1e-6)
    ev = events.for_run("r1")
    assert [e["kind"] for e in ev] == ["model_call"] and ev[0]["ok"] and ev[0]["step"] == "find_cause"
    assert scratch_db["calls"].find_one({"run_id": "r1"})["status"] == "settled"


def test_a_failed_call_is_charged_its_full_reservation(scratch_db, monkeypatch):
    monkeypatch.setattr(models, "_writer", lambda demo, max_tokens: (OPUS, FakeLLM(fail=True)))
    meter.open_run("r1", 0.50, 600)
    with events.bind("r1", "find_cause"), pytest.raises(TimeoutError):
        models.write({"run_id": "r1"}, "find_cause", [("user", "ping")], max_tokens=16)
    s = meter.snapshot("r1")
    assert s["reserved_usd"] == 0 and s["spent_usd"] > 0  # cost unknown → fail closed
    assert events.for_run("r1")[0]["ok"] is False
